"""A fake Waggle phone, for testing the hub's handlers (see WAGGLE.md).

:class:`FakePhone` listens for ``waggle.intent`` and ``waggle.query`` on a bus
and answers each request exactly once, deciding with the same rule matcher as
the hub. What the user would do (accept or decline an ask card, or ignore it)
and what Android would do (launch, find no handler, refuse) are settings, as
are the query fixtures: plain dicts in WAGGLE.md's shapes.

All settings are plain attributes, so a test can change them between requests.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum
from typing import Callable, Optional, Union

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT, CAPABILITIES, CONTACTS_LOOKUP, INTENT, INTENT_RESPONSE, QUERY,
    QUERY_RESPONSE, Capabilities, ErrorCode, Intent, Mode, Query, Response, WaggleError,
    from_wire_time,
)
from waggle.rules import Decision, decide_for, query_enabled

DEFAULT_PEER = "fake-phone"

# The WAGGLE.md example, plus the rest of Wiggins' default rules
# (SHOW_ALARMS, and SENDTO with sms: as well as smsto:).
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
        {"action": "android.intent.action.MAIN", "category": "android.intent.category.LAUNCHER",
         "mode": "run"},
        {"action": "android.intent.action.DIAL", "scheme": "tel", "mode": "ask"},
        {"action": "android.intent.action.SENDTO", "scheme": "smsto", "mode": "ask"},
        {"action": "android.intent.action.SENDTO", "scheme": "sms", "mode": "ask"},
    ],
    "queries": [CALENDAR_NEXT, CONTACTS_LOOKUP, APPS_LIST],
})


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
                 now: Union[datetime, Callable[[], datetime], None] = None):
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
        self.permission_denied = set(permission_denied or ())
        self.now = now
        self.log: list[LogEntry] = []
        self._lock = threading.Lock()
        self._timers: set[threading.Timer] = set()
        self._closed = False
        bus.on(INTENT, self._on_intent)
        bus.on(QUERY, self._on_query)

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
        decision = decide_for(self.capabilities, intent)
        delay = 0.0
        if decision.mode is Mode.BLOCK:
            why = "a rule blocks it" if decision.rule else "no rule matches"
            response = Response.failure(intent.id, ErrorCode.BLOCKED, why)
        elif decision.mode is Mode.ASK and self.ask_answer is AskAnswer.DECLINE:
            response = Response.failure(intent.id, ErrorCode.DECLINED, "the user declined")
        elif decision.mode is Mode.ASK and self.ask_answer is AskAnswer.NONE:
            response = Response.failure(intent.id, ErrorCode.TIMEOUT, "the user didn't answer")
            delay = self.timeout_delay_s
        else:
            outcome = (self.launch_by_action[intent.action]
                       if intent.action in self.launch_by_action else self.launch)
            response = (Response.success(intent.id) if outcome is None
                        else Response.failure(intent.id, outcome, f"launch outcome: {outcome}"))
        self._answer(message, INTENT_RESPONSE, decision, response, delay)

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
        else:
            run = {CALENDAR_NEXT: self._calendar_next, CONTACTS_LOOKUP: self._contacts_lookup,
                   APPS_LIST: self._apps_list}[query.name]
            response = Response.success(query.id, run(query.params))
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
