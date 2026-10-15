"""A fake Waggle phone, for testing the hub's handlers (see WAGGLE.md).

:class:`FakePhone` listens for ``waggle.intent``, ``waggle.query`` and
``waggle.command`` on a bus and answers each request exactly once, deciding
with the same rule matcher as the hub. What the user would do (accept or
decline an ask card, or ignore it) and what Android would do (launch, find no
handler, refuse) are settings, as are the query fixtures: plain dicts in
WAGGLE.md's shapes. Commands change the phone's state (``torch``,
``volumes``, ``clipboard``, and ``status``'s ringer and DND), which
``phone.status`` reads.

All settings are plain attributes, so a test can change them between requests.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum
from typing import Callable, Optional, Union

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT, CAPABILITIES, CLIPBOARD_SET, COMMAND, COMMAND_RESPONSE,
    CONTACTS_LOOKUP, DND_SET, INTENT, INTENT_RESPONSE, NOTIFICATIONS_LIST, PHONE_LOCATION,
    PHONE_SCREEN, PHONE_STATUS, QUERY, QUERY_RESPONSE, READ_ALOUD, REPLY, RINGER_SET, TORCH_SET,
    VOLUME_SET, Capabilities, Command,
    ErrorCode, Intent, Mode, Query, Response, WaggleError, from_wire_time, is_command_action,
)
from waggle.rules import Decision, decide_command, decide_for, query_enabled

DEFAULT_PEER = "fake-phone"

# The WAGGLE.md example, plus the rest of Wiggins' default rules
# (SHOW_ALARMS, SENDTO with sms: as well as smsto:, VIEW with geo:, SEND for sharing, and the
# commands).
DEFAULT_CAPABILITIES = Capabilities.from_dict({
    "version": 1,
    "client": {"name": "waggle-fake-phone", "version": "1"},
    "timezone": "America/Chicago",
    "lang": "en-US",
    "ask_timeout_s": 15,
    "unmatched": "block",
    "rules": [
        {"action": "android.intent.action.SET_ALARM", "mode": "run"},
        {"action": "android.intent.action.SET_TIMER", "mode": "run"},
        {"action": "android.intent.action.SHOW_ALARMS", "mode": "run"},
        {"action": "android.intent.action.SHOW_TIMERS", "mode": "run"},
        {"action": "android.intent.action.DISMISS_TIMER", "mode": "run"},
        {"action": "android.intent.action.SNOOZE_ALARM", "mode": "run"},
        # A wrongly dismissed wake-up alarm costs a lot: the phone asks.
        {"action": "android.intent.action.DISMISS_ALARM", "mode": "ask"},
        {"action": "android.intent.action.MAIN", "category": "android.intent.category.LAUNCHER",
         "mode": "run"},
        {"action": "android.intent.action.DIAL", "scheme": "tel", "mode": "ask"},
        {"action": "android.intent.action.SENDTO", "scheme": "smsto", "mode": "ask"},
        {"action": "android.intent.action.SENDTO", "scheme": "sms", "mode": "ask"},
        {"action": "android.intent.action.VIEW", "scheme": "geo", "mode": "run"},
        # Sharing opens the target app (or the share sheet), where the user sends it.
        {"action": "android.intent.action.SEND", "mode": "run"},
        {"action": TORCH_SET, "mode": "run"},
        {"action": VOLUME_SET, "mode": "run"},
        {"action": RINGER_SET, "mode": "run"},
        {"action": DND_SET, "mode": "run"},
        {"action": READ_ALOUD, "mode": "run"},
        # A reply sends a message: the phone asks.
        {"action": REPLY, "mode": "ask"},
        {"action": CLIPBOARD_SET, "mode": "run"},
    ],
    "queries": [CALENDAR_NEXT, CONTACTS_LOOKUP, APPS_LIST, PHONE_STATUS, NOTIFICATIONS_LIST,
                PHONE_LOCATION, PHONE_SCREEN],
    "commands": [TORCH_SET, VOLUME_SET, RINGER_SET, DND_SET, READ_ALOUD, REPLY, CLIPBOARD_SET],
})

# What phone.status reads before any command changes it.
DEFAULT_STATUS = {"battery": {"percent": 80, "charging": False, "source": "none"},
                  "network": {"type": "wifi", "metered": False},
                  "ringer": "normal", "dnd": False}
# How far one volume step moves, in percent.
VOLUME_STEP = 10


class AskAnswer(str, Enum):
    """What the user does with an ask card."""
    ACCEPT = "accept"
    DECLINE = "decline"
    NONE = "none"  # never answers: the phone sends ``timeout``


@dataclass(frozen=True)
class LogEntry:
    """One request the phone answered (``response`` is None in silent mode)."""
    request: Message
    decision: Optional[Decision]
    response: Optional[Response]


class FakePhone:
    """A Waggle phone on ``bus``, answering requests addressed to ``peer``.

    ``launch`` is the launch outcome for allowed intents: None for success, or
    ``no_handler`` / ``launch_failed``; ``launch_by_action`` overrides it per
    intent action. ``timeout_delay_s`` is how long an unanswered ask card takes
    to time out, and ``response_delay_s`` delays every answer; delayed answers
    are sent from a timer thread, so a hub's wait is tested for real.
    """

    def __init__(self, bus, capabilities: Optional[Capabilities] = None,
                 peer: str = DEFAULT_PEER, ask_answer: AskAnswer = AskAnswer.ACCEPT,
                 timeout_delay_s: float = 0.0, launch: Optional[ErrorCode] = None,
                 launch_by_action: Optional[dict[str, Optional[ErrorCode]]] = None,
                 silent: bool = False, response_delay_s: float = 0.0,
                 events: Optional[list[dict]] = None, contacts: Optional[list[dict]] = None,
                 apps: Optional[list[dict]] = None,
                 permission_denied: Optional[set[str]] = None,
                 now: Union[datetime, Callable[[], datetime], None] = None,
                 status: Optional[dict] = None, notifications: Optional[list[dict]] = None,
                 location: Optional[dict] = None, screen: Optional[dict] = None):
        self.bus = bus
        self.capabilities = capabilities or DEFAULT_CAPABILITIES
        self.peer = peer
        self.ask_answer = AskAnswer(ask_answer)
        self.timeout_delay_s = timeout_delay_s
        self.launch = launch
        self.launch_by_action = dict(launch_by_action or {})
        self.silent = silent
        self.response_delay_s = response_delay_s
        self.events = list(events or [])
        self.contacts = list(contacts or [])
        self.apps = list(apps or [])
        # Query and command names the phone lacks Android access for.
        self.permission_denied = set(permission_denied or ())
        self.now = now
        self.status = json.loads(json.dumps(status if status is not None else DEFAULT_STATUS))
        self.notifications = list(notifications or [])
        # phone.location's answer; None answers timeout, as a phone without a fix would.
        self.location = location
        # phone.screen's answer, screenshot included (left out unless asked for); None
        # answers not_found, as a phone with no snapshot would.
        self.screen = screen
        self.torch = False
        self.clipboard: Optional[str] = None
        # What read_aloud read (the matching notifications) and the replies sent.
        self.read: list[list[dict]] = []
        self.replies: list[dict] = []
        self.volumes = {"media": 50, "ring": 50, "alarm": 50, "call": 50}
        self.log: list[LogEntry] = []
        self._lock = threading.Lock()
        self._timers: set[threading.Timer] = set()
        self._closed = False
        bus.on(INTENT, self._on_intent)
        bus.on(QUERY, self._on_query)
        bus.on(COMMAND, self._on_command)

    # --- public --------------------------------------------------------------

    def announce(self, origin: Optional[Message] = None) -> Message:
        """Emit ``waggle.capabilities``, keeping ``origin``'s context (e.g. its session)."""
        message = Message(CAPABILITIES, self.capabilities.to_dict(), self._context(origin))
        self.bus.emit(message)
        return message

    @property
    def responses(self) -> list[Response]:
        return [e.response for e in self.log if e.response is not None]

    def close(self) -> None:
        """Stop answering: remove the handlers and drop any delayed answers."""
        with self._lock:
            self._closed = True
            timers, self._timers = self._timers, set()
        for timer in timers:
            timer.cancel()
        self.bus.remove(INTENT, self._on_intent)
        self.bus.remove(QUERY, self._on_query)
        self.bus.remove(COMMAND, self._on_command)

    # --- requests ------------------------------------------------------------

    def _for_me(self, message: Message) -> bool:
        destination = message.context.get("destination")
        if destination is None:
            return True
        if isinstance(destination, list):
            return self.peer in destination
        return destination == self.peer

    def _on_intent(self, message: Message) -> None:
        if not self._for_me(message):
            return
        try:
            intent = Intent.from_dict(message.data)
        except WaggleError as e:
            self._answer(message, INTENT_RESPONSE, None,
                         Response.failure(_request_id(message), e.code, e.message))
            return
        if is_command_action(intent.action):
            self._answer(message, INTENT_RESPONSE, None, Response.failure(
                intent.id, ErrorCode.BAD_REQUEST, "that action names a command"))
            return
        decision = decide_for(self.capabilities, intent)
        response, delay = self._decided(intent.id, decision)
        if response is None:
            outcome = (self.launch_by_action[intent.action]
                       if intent.action in self.launch_by_action else self.launch)
            response = (Response.success(intent.id) if outcome is None
                        else Response.failure(intent.id, outcome, f"launch outcome: {outcome}"))
        self._answer(message, INTENT_RESPONSE, decision, response, delay)

    def _decided(self, request_id: str, decision: Decision) -> tuple[Optional[Response], float]:
        """The answer when the rules or the user refuse, and its delay; None to go ahead."""
        if decision.mode is Mode.BLOCK:
            why = "a rule blocks it" if decision.rule else "no rule matches"
            return Response.failure(request_id, ErrorCode.BLOCKED, why), 0.0
        if decision.mode is Mode.ASK and self.ask_answer is AskAnswer.DECLINE:
            return Response.failure(request_id, ErrorCode.DECLINED, "the user declined"), 0.0
        if decision.mode is Mode.ASK and self.ask_answer is AskAnswer.NONE:
            return (Response.failure(request_id, ErrorCode.TIMEOUT, "the user didn't answer"),
                    self.timeout_delay_s)
        return None, 0.0

    def _on_command(self, message: Message) -> None:
        if not self._for_me(message):
            return
        try:
            command = Command.from_dict(message.data)
        except WaggleError as e:
            self._answer(message, COMMAND_RESPONSE, None,
                         Response.failure(_request_id(message), e.code, e.message))
            return
        target = None
        if command.name == REPLY:
            target, failure = self._reply_target(command)
            if failure is not None:
                self._answer(message, COMMAND_RESPONSE, None, failure)
                return
        decision = decide_command(self.capabilities, command,
                                  target["package"] if target else None)
        response, delay = self._decided(command.id, decision)
        if response is None and target is not None:
            self.replies.append({"ref": target["ref"], "text": command.params["text"]})
            response = Response.success(command.id)
        if response is None:
            if command.name in self.permission_denied:
                response = Response.failure(command.id, ErrorCode.PERMISSION_DENIED,
                                            f"no access for {command.name}")
            else:
                response = Response.success(command.id, self._run_command(command))
        self._answer(message, COMMAND_RESPONSE, decision, response, delay)

    def _reply_target(self, command: Command) -> tuple[Optional[dict], Optional[Response]]:
        """The notification a reply goes to, or the failure to answer with."""
        p = command.params
        if "ref" in p:
            found = [n for n in self.notifications if n.get("ref") == p["ref"]]
        else:
            found = self._matching({"from": p["to"]})
        found = [n for n in found if n.get("can_reply")]
        conversations = {(n.get("package"), n.get("title")) for n in found}
        if not found:
            return None, Response.failure(command.id, ErrorCode.NOT_FOUND, "no such conversation")
        if len(conversations) > 1:
            return None, Response.failure(command.id, ErrorCode.AMBIGUOUS,
                                          f"{len(conversations)} conversations match")
        return found[0], None

    def _matching(self, params: dict) -> list[dict]:
        """Notifications matching ``app``, ``from`` and ``since``, newest first."""
        app = (params.get("app") or "").casefold()
        sender = (params.get("from") or "").casefold()
        since = from_wire_time(params["since"]) if params.get("since") else None
        found = []
        for n in self.notifications:
            senders = [n.get("title") or ""] + [m.get("sender") or ""
                                                for m in n.get("messages") or []]
            if app and app not in (n.get("app") or "").casefold() \
                    and app not in (n.get("package") or "").casefold():
                continue
            if sender and not any(sender in s.casefold() for s in senders):
                continue
            if since is not None and n.get("time") and from_wire_time(n["time"]) < since:
                continue
            found.append(n)
        return sorted(found, key=lambda n: n.get("time") or "", reverse=True)

    def _run_command(self, command: Command) -> Optional[dict]:
        p = command.params
        if command.name == READ_ALOUD:
            read = self._matching(p)[:p.get("limit", 5)]
            self.read.append(read)
            return {"count": len(read)}
        if command.name == CLIPBOARD_SET:
            self.clipboard = p["text"]
        elif command.name == TORCH_SET:
            self.torch = p["on"]
        elif command.name == RINGER_SET:
            self.status["ringer"] = p["mode"]
        elif command.name == DND_SET:
            self.status["dnd"] = p["on"]
        elif command.name == VOLUME_SET:
            stream = p.get("stream", "media")
            if "level" in p:
                level = p["level"]
            else:
                step = VOLUME_STEP if p["step"] == "up" else -VOLUME_STEP
                level = max(0, min(100, self.volumes[stream] + step))
            self.volumes[stream] = level
            return {"level": level}
        return None

    def _on_query(self, message: Message) -> None:
        if not self._for_me(message):
            return
        try:
            query = Query.from_dict(message.data)
        except WaggleError as e:
            self._answer(message, QUERY_RESPONSE, None,
                         Response.failure(_request_id(message), e.code, e.message))
            return
        if not query_enabled(self.capabilities, query.name):
            response = Response.failure(query.id, ErrorCode.BLOCKED, f"{query.name} isn't enabled")
        elif query.name in self.permission_denied:
            response = Response.failure(query.id, ErrorCode.PERMISSION_DENIED,
                                        f"no permission for {query.name}")
        elif query.name == PHONE_SCREEN:
            response = (Response.success(query.id, self._phone_screen(query.params))
                        if self.screen is not None
                        else Response.failure(query.id, ErrorCode.NOT_FOUND, "no snapshot"))
        else:
            run = {CALENDAR_NEXT: self._calendar_next, CONTACTS_LOOKUP: self._contacts_lookup,
                   APPS_LIST: self._apps_list, PHONE_STATUS: self._phone_status,
                   NOTIFICATIONS_LIST: self._notifications_list,
                   PHONE_LOCATION: self._phone_location}[query.name]
            data = run(query.params)
            response = (Response.success(query.id, data) if data is not None
                        else Response.failure(query.id, ErrorCode.TIMEOUT, "no fix"))
        self._answer(message, QUERY_RESPONSE, None, response)

    def _answer(self, request: Message, response_type: str, decision: Optional[Decision],
                response: Response, extra_delay_s: float = 0.0) -> None:
        if self.silent:
            LOG.debug(f"{self.peer}: silently dropping {request.msg_type}")
            self._record(request, decision, None)
            return
        delay = self.response_delay_s + extra_delay_s
        if delay <= 0:
            self._send(request, response_type, decision, response)
            return

        def send_later():
            with self._lock:
                self._timers.discard(timer)
                if self._closed:
                    return
            self._send(request, response_type, decision, response)

        timer = threading.Timer(delay, send_later)
        timer.daemon = True
        with self._lock:
            if self._closed:
                return
            self._timers.add(timer)
        timer.start()

    def _send(self, request: Message, response_type: str, decision: Optional[Decision],
              response: Response) -> None:
        self._record(request, decision, response)
        self.bus.emit(Message(response_type, response.to_dict(), self._context(request)))

    def _record(self, request: Message, decision: Optional[Decision],
                response: Optional[Response]) -> None:
        with self._lock:
            self.log.append(LogEntry(request, decision, response))

    def _context(self, origin: Optional[Message]) -> dict:
        # Messages from a phone are new messages, not replies. hivemind-core 3.4
        # (handle_inject_agent_msg) sets source and peer to the client's peer and
        # destination to "skills", so the hub's Message.reply routes back here.
        context = {"source": self.peer, "peer": self.peer, "destination": "skills"}
        if origin is not None and "session" in origin.context:
            context["session"] = origin.context["session"]
        return context

    # --- queries -------------------------------------------------------------

    def _current_time(self) -> datetime:
        now = self.now() if callable(self.now) else self.now
        return now or datetime.now(timezone.utc)

    def _phone_tz(self) -> tzinfo:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.capabilities.timezone) if self.capabilities.timezone else timezone.utc
        except Exception:
            return timezone.utc

    def _calendar_next(self, params: dict) -> dict:
        """Events not yet over that start within ``within_days``, in start order."""
        now = self._current_time()
        horizon = now + timedelta(days=params.get("within_days", 7))
        tz = self._phone_tz()
        upcoming = []
        for event in self.events:
            start, end = _event_bounds(event, tz)
            if (end > now or start >= now) and start < horizon:
                upcoming.append((start, event))
        upcoming.sort(key=lambda pair: pair[0])
        return {"events": [dict(e) for _, e in upcoming[:params.get("count", 1)]]}

    def _contacts_lookup(self, params: dict) -> dict:
        """Contacts whose ``fn`` or ``nickname`` contains ``name``; nicknames aren't sent."""
        needle = params["name"].casefold()
        matches = []
        for contact in self.contacts:
            nicknames = contact.get("nickname") or []
            names = [contact.get("fn") or ""] + ([nicknames] if isinstance(nicknames, str)
                                                 else list(nicknames))
            if any(needle in n.casefold() for n in names):
                matches.append({k: v for k, v in contact.items() if k != "nickname"})
        return {"contacts": matches[:params.get("limit", 5)]}

    def _notifications_list(self, params: dict) -> dict:
        return {"notifications": [dict(n) for n in
                                  self._matching(params)[:params.get("limit", 10)]]}

    def _phone_location(self, params: dict) -> Optional[dict]:
        return dict(self.location) if self.location is not None else None

    def _phone_screen(self, params: dict) -> dict:
        screen = json.loads(json.dumps(self.screen))
        if not params.get("image"):
            screen.pop("image", None)
        return screen

    def _phone_status(self, params: dict) -> dict:
        return json.loads(json.dumps(self.status))

    def _apps_list(self, params: dict) -> dict:
        """Apps whose ``label`` contains ``name`` (all of them without one)."""
        needle = (params.get("name") or "").casefold()
        matches = [dict(a) for a in self.apps if needle in (a.get("label") or "").casefold()]
        return {"apps": matches[:params.get("limit", 20)]}


def _request_id(message: Message) -> str:
    """The id to answer a malformed request with: a string id as given, else ``""``."""
    data = message.data
    request_id = data.get("id") if isinstance(data, dict) else None
    return request_id if isinstance(request_id, str) else ""


def _as_datetime(value: datetime | date, tz: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time(), tzinfo=tz)  # all-day: midnight on the phone


def _event_bounds(event: dict, tz: tzinfo) -> tuple[datetime, datetime]:
    start = from_wire_time(event["dtstart"])
    if event.get("dtend"):
        end = from_wire_time(event["dtend"])
    else:
        end = start + timedelta(days=1) if not isinstance(start, datetime) else start
    return _as_datetime(start, tz), _as_datetime(end, tz)
