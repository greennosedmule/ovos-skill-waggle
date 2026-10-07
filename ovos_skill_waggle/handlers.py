"""The request handlers: run a ``waggle:request`` on the phone (see SPEC.md "Request/response flow").

:class:`RequestHandlers` takes a request message, finds the asking phone's
capabilities, re-checks the phone's rules, looks things up on the phone when
the request needs it, sends the Waggle intent, waits for the answer and
reports the outcome: always as a ``waggle:request.response`` (a reply to the
request), and also spoken when the request says ``speak``. It never sees an
utterance; when a lookup finds several matches it asks which one through the
``ask`` callback it's given, and reads the answer with the parsing module's
:func:`~ovos_skill_waggle.parsing.choose`.

Routing: a request reaches the phone only if the bus message sending it is
addressed to the phone's peer. The request message may be the utterance's
own context (source = the peer, as SPEC's ``waggle:request`` asks) or
ovos-core's match message, which is already a reply to the utterance
(destination = the peer). :func:`phone_origin` turns either into a stand-in
for the message the phone sent, so ``Message.reply`` (which S1's
``WaggleClient`` uses) addresses the phone in both cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle import intents, queries
from waggle.client import WaggleClient
from waggle.messages import ErrorCode, Intent, Mode, Query, Response, WaggleError
from waggle.queries import App, Contact
from waggle.rules import decide_for

from ovos_skill_waggle import agenda
from ovos_skill_waggle.actions import (
    INTENT_BUILDERS, SUPPORTED_REQUESTS, build_intent, precheck_intent, required_query,
    spoken_number,
)
from ovos_skill_waggle.capabilities import CapabilityCache, Phone, peer_of
from ovos_skill_waggle.parsing import PHONE_TYPE_SYNONYMS, choose
from ovos_skill_waggle.requests import (
    ALARM_SET, ALARMS_SHOW, APP_OPEN, CALENDAR_NEXT, CONTACT_CALL, MESSAGE_COMPOSE, TIMER_SET,
    UNREACHABLE, RequestResponse, WaggleRequest, response_message,
)
from ovos_skill_waggle.settings import Settings

# The context value hivemind-core gives client messages that name no destination.
HUB_DESTINATION = "skills"

# Phone error code -> dialog. Codes a newer phone adds fall back to "phone_error".
ERROR_DIALOGS = {
    ErrorCode.DECLINED.value: "declined",
    ErrorCode.BLOCKED.value: "blocked",
    ErrorCode.TIMEOUT.value: "ask_timeout",
    ErrorCode.NO_HANDLER.value: "no_handler",
    ErrorCode.LAUNCH_FAILED.value: "launch_failed",
    ErrorCode.PERMISSION_DENIED.value: "permission_denied",
    ErrorCode.UNSUPPORTED_VERSION.value: "unsupported_version",
    ErrorCode.BAD_REQUEST.value: "phone_error",
}

# What each query reads, for "Your phone doesn't share … with the hub".
QUERY_SUBJECTS = {"calendar.next": "your calendar", "contacts.lookup": "your contacts",
                  "apps.list": "its list of apps"}

# The most matches read out in a "which one?" question; more is too many to hear.
MAX_CHOICES = 4
# How many upcoming events a "today" or "Friday" question fetches (WAGGLE.md's maximum).
DAY_EVENTS = 10
# calendar.next's window when no day is asked about.
CALENDAR_WITHIN_DAYS = 7
# Lookups ask the phone for at most this many matches.
LOOKUP_LIMIT = 10


def phone_origin(message: Message, peer: str) -> Message:
    """A stand-in for the message ``peer`` sent, so its ``reply`` is addressed to ``peer``.

    Keeps the context (the session above all) and only sets ``source`` to the
    peer and ``destination`` to the hub side of the conversation.
    """
    context = dict(message.context or {})
    if context.get("destination") == peer:  # already a reply to the peer
        hub = context.get("source")
    else:
        hub = context.get("destination")
    context["source"] = peer
    context["destination"] = hub if isinstance(hub, str) and hub and hub != peer else HUB_DESTINATION
    return Message(message.msg_type, message.data, context)


# --- spoken forms ------------------------------------------------------------

def spoken_clock(hour: int, minute: int) -> str:
    """12-hour clock text: 6:30 -> "6:30 AM", 0:00 -> "12:00 AM"."""
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def spoken_duration(seconds: int) -> str:
    """ "10 minutes", "1 hour and 30 minutes", "2 hours, 15 minutes and 10 seconds"."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    words = [f"{n} {unit}{'' if n == 1 else 's'}"
             for n, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if n]
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} and {words[-1]}"


def as_name(said: str) -> str:
    """A name as parsed ("sam", "sam lee") written as a name, unless it already has capitals."""
    return said if said != said.lower() else said.title()


def spoken_choices(options: list[str]) -> str:
    """ "A or B", "A, B or C"."""
    if len(options) <= 1:
        return "".join(options)
    return f"{', '.join(options[:-1])} or {options[-1]}"


# --- outcomes ----------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """A result, as a dialog to render into the response's summary (and maybe speak)."""
    dialog: str
    data: dict = field(default_factory=dict)
    error: Optional[str] = None  # None for success

    @classmethod
    def failure(cls, error: str, dialog: str, **data) -> "Outcome":
        return cls(dialog, data, error)

    @property
    def ok(self) -> bool:
        return self.error is None


def success_outcome(request: WaggleRequest) -> Outcome:
    """The confirmation for an intent-only request."""
    p = request.params
    if request.request == ALARM_SET:
        data = {"time": spoken_clock(p["hour"], p["minute"])}
        if p.get("label"):
            return Outcome("alarm_set_label", {**data, "label": p["label"]})
        return Outcome("alarm_set", data)
    if request.request == TIMER_SET:
        data = {"duration": spoken_duration(p["seconds"])}
        if p.get("label"):
            return Outcome("timer_set_label", {**data, "label": p["label"]})
        return Outcome("timer_set", data)
    if request.request == ALARMS_SHOW:
        return Outcome("alarms_show")
    return Outcome("done")


def failure_outcome(response: Optional[Response]) -> Outcome:
    """The outcome of a phone's failed answer (None: no answer in time)."""
    if response is None:
        return Outcome.failure(UNREACHABLE, "unreachable")
    code = response.error.value if isinstance(response.error, ErrorCode) else str(response.error)
    if code in ERROR_DIALOGS:
        return Outcome.failure(code, ERROR_DIALOGS[code])
    # An error code from a newer phone: report it as a phone-side failure.
    LOG.warning(f"Waggle: unknown error code {code!r} from the phone")
    return Outcome.failure(ErrorCode.BAD_REQUEST.value, "phone_error")


def response_outcome(success: Outcome, response: Optional[Response]) -> Outcome:
    """``success`` if the phone ran the intent, else the failure it reported."""
    return success if response is not None and response.ok else failure_outcome(response)


class _Stop(Exception):
    """Ends a request early with an outcome (a lookup found nothing, the user said no)."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.dialog)
        self.outcome = outcome


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


# --- handlers ----------------------------------------------------------------

Render = Callable[[str, dict], str]
Speak = Callable[[Message, str, str, dict], None]  # (origin to reply to, text, dialog, data)
# (origin to reply to, dialog, data) -> the user's answer, or None (no answer, or "cancel")
Ask = Callable[[Message, str, dict], Optional[str]]


class RequestHandlers:
    """Runs ``waggle:request`` messages. Thread-safe: each call has its own state.

    ``settings`` is called for each request so that config changes apply.
    ``render`` turns a dialog name and data into words; ``speak`` emits them,
    as a reply to the origin it's given; ``ask`` speaks a question and returns
    the answer. ``clock`` is the current time (aware), for calendar days.
    """

    def __init__(self, bus, cache: CapabilityCache, settings: Callable[[], Settings],
                 render: Render, speak: Speak, ask: Optional[Ask] = None,
                 clock: Optional[Callable[[], datetime]] = None):
        self.bus = bus
        self.cache = cache
        self.settings = settings
        self.render = render
        self.speak = speak
        self.ask = ask or (lambda origin, dialog, data: None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client = WaggleClient(bus)

    def handle(self, message: Message) -> RequestResponse:
        """Run the request in ``message``; emits and returns the ``waggle:request.response``."""
        data = message.data if isinstance(message.data, dict) else {}
        speak = data.get("speak", True) is True
        peer = peer_of(message)
        origin = phone_origin(message, peer) if peer else message
        try:
            request = WaggleRequest.from_dict(data)
        except WaggleError as e:
            LOG.warning(f"Waggle: bad request from {peer}: {e.message}")
            outcome = Outcome.failure(ErrorCode.BAD_REQUEST.value, "error")
        else:
            speak = request.speak
            try:
                outcome = self._run(request, origin, peer)
            except _Stop as stop:
                outcome = stop.outcome
            except Exception as e:  # a bug here must still answer the caller
                LOG.exception(f"Waggle: {request.request} failed: {e}")
                outcome = Outcome.failure(ErrorCode.BAD_REQUEST.value, "error")
        return self._report(message, origin, outcome, speak)

    # --- the flow ------------------------------------------------------------

    def _run(self, request: WaggleRequest, origin: Message, peer: Optional[str]) -> Outcome:
        settings = self.settings()
        if not settings.enabled(request.request):
            return Outcome.failure(ErrorCode.BLOCKED.value, "disabled")
        if request.request not in SUPPORTED_REQUESTS:
            return Outcome.failure(ErrorCode.BAD_REQUEST.value, "not_supported")

        phone = self.cache.get(peer)
        if phone is None:
            if self.cache.rejected_version(peer) is not None:
                return Outcome.failure(ErrorCode.UNSUPPORTED_VERSION.value, "unsupported_version")
            return Outcome.failure(UNREACHABLE, "not_connected")

        self._precheck(request, phone, settings)
        if request.request in INTENT_BUILDERS:
            return self._launch(origin, request, phone, settings, build_intent(request, settings),
                                success_outcome(request))
        run = {CALENDAR_NEXT: self._calendar_next, APP_OPEN: self._app_open,
               CONTACT_CALL: self._contact_call,
               MESSAGE_COMPOSE: self._message_compose}[request.request]
        return run(request, origin, phone, settings)

    def _precheck(self, request: WaggleRequest, phone: Phone, settings: Settings) -> None:
        """Stop unless the phone's announced queries and rules allow the request."""
        query = required_query(request)
        if query is not None and query not in phone.capabilities.queries:
            raise _Stop(Outcome.failure(ErrorCode.BLOCKED.value, "query_off",
                                        what=QUERY_SUBJECTS.get(query, "that")))
        try:
            intent = precheck_intent(request, settings)
        except ValueError as e:
            LOG.warning(f"Waggle: can't build {request.request}: {e}")
            raise _Stop(Outcome.failure(ErrorCode.BAD_REQUEST.value, "error"))
        if intent is not None and not decide_for(phone.capabilities, intent).allowed:
            raise _Stop(Outcome.failure(ErrorCode.BLOCKED.value, "blocked"))

    def _launch(self, origin: Message, request: WaggleRequest, phone: Phone, settings: Settings,
                intent: Intent, success: Outcome) -> Outcome:
        """Send ``intent`` and wait for the phone; ``success`` if it ran."""
        decision = decide_for(phone.capabilities, intent)
        if not decision.allowed:
            return Outcome.failure(ErrorCode.BLOCKED.value, "blocked")
        timeout_s = self._timeout(decision.mode, phone, settings)
        if decision.mode is Mode.ASK and request.speak:
            self._say(origin, "confirm_on_phone", {})
        LOG.info(f"Waggle: {request.request} -> {phone.peer} ({intent.action}, "
                 f"{decision.mode.value}, waiting {timeout_s}s)")
        response = self.client.send_intent(origin, intent, timeout_s)
        return response_outcome(success, response)

    def _query(self, origin: Message, phone: Phone, settings: Settings, query: Query) -> dict:
        """Run ``query`` on the phone; its data, or stop with the failure."""
        LOG.info(f"Waggle: {query.name} -> {phone.peer}")
        response = self.client.send_query(origin, query, settings.response_timeout_s)
        if response is None or not response.ok:
            outcome = failure_outcome(response)
            if outcome.error == ErrorCode.BLOCKED.value:
                outcome = Outcome.failure(outcome.error, "query_off",
                                          what=QUERY_SUBJECTS.get(query.name, "that"))
            raise _Stop(outcome)
        return response.data or {}

    @staticmethod
    def _timeout(mode: Mode, phone: Phone, settings: Settings) -> float:
        if mode is Mode.ASK:
            return settings.ask_wait_s(phone.capabilities.ask_timeout_s)
        return settings.response_timeout_s

    # --- choosing --------------------------------------------------------------

    def _pick(self, origin: Message, options: list[str], dialog: str, data: dict,
              synonyms: Optional[dict[str, str]] = None) -> int:
        """Ask which of ``options`` (two or more) the user means; its index, or stop."""
        options = options[:MAX_CHOICES]
        data = {**data, "options": spoken_choices(options)}
        for attempt in range(2):
            answer = self.ask(origin, dialog if attempt == 0 else "pick_again", data)
            if answer is None:
                raise _Stop(Outcome.failure(ErrorCode.DECLINED.value, "declined"))
            index = choose(answer, options, synonyms)
            if index is not None:
                return index
        raise _Stop(Outcome.failure(ErrorCode.DECLINED.value, "not_understood"))

    def _pick_by_name(self, origin: Message, found: list, name: str, label: Callable[[object], str],
                      dialog: str, data: dict) -> object:
        """The one of ``found`` the user means by ``name``: an exact name, the only match, or asked."""
        exact = [item for item in found if _folded(label(item)) == _folded(name)]
        if len(exact) == 1:
            return exact[0]
        if len(found) == 1:
            return found[0]
        choices = exact or found
        labels = [label(item) for item in choices]
        if len(set(map(_folded, labels))) < len(labels):
            # Two with the same name can't be told apart by voice; take the first.
            return choices[0]
        return choices[self._pick(origin, labels, dialog, data)]

    # --- calendar.next ---------------------------------------------------------

    def _calendar_next(self, request: WaggleRequest, origin: Message, phone: Phone,
                       settings: Settings) -> Outcome:
        tz = phone.tz or timezone.utc
        now = self.clock().astimezone(tz)
        day = request.params.get("day")
        if day is not None:
            day = date.fromisoformat(day)
            within = max(1, (day - now.date()).days + 1)
            query = queries.calendar_next(DAY_EVENTS, within)
        else:
            query = queries.calendar_next(request.params.get("count", 1), CALENDAR_WITHIN_DAYS)
        data = self._query(origin, phone, settings, query)
        try:
            events = queries.parse_calendar_next(data)
        except WaggleError as e:
            LOG.warning(f"Waggle: bad calendar.next result from {phone.peer}: {e.message}")
            return Outcome.failure(ErrorCode.BAD_REQUEST.value, "phone_error")

        if day is not None:
            spoken = agenda.spoken_day(day, now.date())
            events = agenda.on_day(events, day, tz)
            if not events:
                return Outcome("calendar_none_day", {"day": spoken})
            return Outcome("calendar_day", {
                "day": spoken, "count": len(events),
                "events": agenda.spoken_list([agenda.spoken_event(e, now, tz, with_day=False)
                                              for e in events])})
        if not events:
            return Outcome("calendar_none")
        if len(events) == 1:
            return Outcome("calendar_next", {
                "event": agenda.spoken_event(events[0], now, tz, with_location=True)})
        return Outcome("calendar_list", {
            "count": len(events),
            "events": agenda.spoken_list([agenda.spoken_event(e, now, tz) for e in events])})

    # --- app.open --------------------------------------------------------------

    def _app_open(self, request: WaggleRequest, origin: Message, phone: Phone,
                  settings: Settings) -> Outcome:
        name = request.params["name"]
        data = self._query(origin, phone, settings, queries.apps_list(name, LOOKUP_LIMIT))
        apps = self._parsed(queries.parse_apps, data, phone)
        if not apps:
            return Outcome.failure(ErrorCode.NO_HANDLER.value, "app_not_found", name=name)
        app: App = self._pick_by_name(origin, apps, name, lambda a: a.label, "which_app", {})
        return self._launch(origin, request, phone, settings,
                            intents.launch_app(app.package, label=app.label),
                            Outcome("app_opened", {"app": app.label}))

    # --- contact.call and message.compose --------------------------------------

    def _contact_call(self, request: WaggleRequest, origin: Message, phone: Phone,
                      settings: Settings) -> Outcome:
        who, number = self._number(request, origin, phone, settings, prefer="cell")
        intent = intents.dial(number, name=who, package=settings.package(CONTACT_CALL))
        return self._launch(origin, request, phone, settings, intent,
                            Outcome("contact_dialed", {"name": who}))

    def _message_compose(self, request: WaggleRequest, origin: Message, phone: Phone,
                         settings: Settings) -> Outcome:
        who, number = self._number(request, origin, phone, settings, prefer="cell",
                                   texting=True)
        intent = intents.compose_sms(number, request.params["body"], name=who,
                                     package=settings.package(MESSAGE_COMPOSE))
        return self._launch(origin, request, phone, settings, intent,
                            Outcome("message_ready", {"name": who}))

    def _number(self, request: WaggleRequest, origin: Message, phone: Phone, settings: Settings,
                prefer: str, texting: bool = False) -> tuple[str, str]:
        """Who and which number a call or text goes to: said aloud, or looked up and chosen."""
        name = request.params["name"]
        said = spoken_number(name)
        if said:
            return name.strip(), said
        data = self._query(origin, phone, settings, queries.contacts_lookup(name, LOOKUP_LIMIT))
        contacts = self._parsed(queries.parse_contacts, data, phone)
        if not contacts:
            raise _Stop(Outcome.failure(ErrorCode.NO_HANDLER.value, "contact_not_found",
                                        name=name))
        contact: Contact = self._pick_by_name(origin, contacts, name, lambda c: c.fn,
                                              "which_contact", {"name": as_name(name)})
        return contact.fn, self._pick_number(origin, contact, request.params.get("type"),
                                             texting)

    def _pick_number(self, origin: Message, contact: Contact, wanted: Optional[str],
                     texting: bool) -> str:
        numbers = list(contact.tel)
        if not numbers:
            raise _Stop(Outcome.failure(ErrorCode.NO_HANDLER.value, "contact_no_number",
                                        name=contact.fn))
        if wanted:
            numbers = [p for p in numbers if p.type == wanted]
            if not numbers:
                raise _Stop(Outcome.failure(ErrorCode.NO_HANDLER.value, "contact_no_type",
                                            name=contact.fn, type=wanted))
        if texting:
            # Texts go to a mobile number when there's exactly one.
            cells = [p for p in numbers if p.type == "cell"]
            if len(cells) == 1:
                return cells[0].value
        if len(numbers) == 1:
            return numbers[0].value
        labels = [p.type for p in numbers]
        if len(set(labels)) < len(labels):
            labels = [f"{p.type} ending in {' '.join(p.value[-4:])}" for p in numbers]
        index = self._pick(origin, labels, "which_number", {"name": contact.fn},
                           PHONE_TYPE_SYNONYMS)
        return numbers[index].value

    @staticmethod
    def _parsed(parse: Callable[[dict], list], data: dict, phone: Phone) -> list:
        try:
            return parse(data)
        except WaggleError as e:
            LOG.warning(f"Waggle: bad query result from {phone.peer}: {e.message}")
            raise _Stop(Outcome.failure(ErrorCode.BAD_REQUEST.value, "phone_error"))

    # --- speaking --------------------------------------------------------------

    def _say(self, origin: Message, dialog: str, data: dict) -> str:
        text = self.render(dialog, data)
        self.speak(origin, text, dialog, data)
        return text

    def _report(self, message: Message, origin: Message, outcome: Outcome,
                speak: bool) -> RequestResponse:
        summary = self.render(outcome.dialog, outcome.data)
        if speak:
            self.speak(origin, summary, outcome.dialog, outcome.data)
        response = (RequestResponse.success(summary) if outcome.error is None
                    else RequestResponse.failure(outcome.error, summary))
        self.bus.emit(response_message(message, response))
        return response
