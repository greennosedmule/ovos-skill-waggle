"""The request handlers: run a ``waggle:request`` on the phone (see SPEC.md "Request/response flow").

:class:`RequestHandlers` takes a request message, finds the asking phone's
capabilities, re-checks the phone's rules, sends the Waggle intent, waits for
the answer and reports the outcome: always as a ``waggle:request.response``
(a reply to the request), and also spoken when the request says ``speak``.
It never sees an utterance.

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
from typing import Callable, Optional

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.client import WaggleClient
from waggle.messages import ErrorCode, Mode, Response, WaggleError
from waggle.rules import decide_for

from ovos_skill_waggle.actions import SUPPORTED_REQUESTS, build_intent
from ovos_skill_waggle.capabilities import CapabilityCache, Phone, peer_of
from ovos_skill_waggle.requests import (
    ALARM_SET, ALARMS_SHOW, TIMER_SET, UNREACHABLE, RequestResponse, WaggleRequest,
    response_message,
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


def success_outcome(request: WaggleRequest) -> Outcome:
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


def response_outcome(request: WaggleRequest, response: Optional[Response]) -> Outcome:
    """The outcome of the phone's answer to a request's intent (None: no answer in time)."""
    if response is None:
        return Outcome.failure(UNREACHABLE, "unreachable")
    if response.ok:
        return success_outcome(request)
    code = response.error.value if isinstance(response.error, ErrorCode) else str(response.error)
    if code in ERROR_DIALOGS:
        return Outcome.failure(code, ERROR_DIALOGS[code])
    # An error code from a newer phone: report it as a phone-side failure.
    LOG.warning(f"Waggle: unknown error code {code!r} from the phone")
    return Outcome.failure(ErrorCode.BAD_REQUEST.value, "phone_error")


# --- handlers ----------------------------------------------------------------

Render = Callable[[str, dict], str]
Speak = Callable[[Message, str, str, dict], None]  # (origin to reply to, text, dialog, data)


class RequestHandlers:
    """Runs ``waggle:request`` messages. Thread-safe: each call has its own state.

    ``settings`` is called for each request so that config changes apply.
    ``render`` turns a dialog name and data into words; ``speak`` emits them,
    as a reply to the origin it's given.
    """

    def __init__(self, bus, cache: CapabilityCache, settings: Callable[[], Settings],
                 render: Render, speak: Speak):
        self.bus = bus
        self.cache = cache
        self.settings = settings
        self.render = render
        self.speak = speak
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
                outcome = self._run(request, message, origin, peer)
            except Exception as e:  # a bug here must still answer the caller
                LOG.exception(f"Waggle: {request.request} failed: {e}")
                outcome = Outcome.failure(ErrorCode.BAD_REQUEST.value, "error")
        return self._report(message, origin, outcome, speak)

    def _run(self, request: WaggleRequest, message: Message, origin: Message,
             peer: Optional[str]) -> Outcome:
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

        try:
            intent = build_intent(request, settings)
        except ValueError as e:
            LOG.warning(f"Waggle: can't build {request.request}: {e}")
            return Outcome.failure(ErrorCode.BAD_REQUEST.value, "error")

        decision = decide_for(phone.capabilities, intent)
        if not decision.allowed:
            return Outcome.failure(ErrorCode.BLOCKED.value, "blocked")
        timeout_s = self._timeout(decision.mode, phone, settings)
        if decision.mode is Mode.ASK and request.speak:
            self._say(origin, "confirm_on_phone", {})

        LOG.info(f"Waggle: {request.request} -> {phone.peer} ({intent.action}, "
                 f"{decision.mode.value}, waiting {timeout_s}s)")
        response = self.client.send_intent(origin, intent, timeout_s)
        return response_outcome(request, response)

    @staticmethod
    def _timeout(mode: Mode, phone: Phone, settings: Settings) -> float:
        if mode is Mode.ASK:
            return settings.ask_wait_s(phone.capabilities.ask_timeout_s)
        return settings.response_timeout_s

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
