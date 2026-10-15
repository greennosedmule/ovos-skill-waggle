"""The ``waggle:request`` and ``waggle:request.response`` shapes (see SPEC.md "Requests").

A request is a structured ask, such as ``timer.set {seconds: 600}``, from the
pipeline stage, the persona's tool calls or another skill. The handler that
runs it answers with a :class:`RequestResponse` as a reply to the request.
Malformed requests and responses raise :class:`~waggle.messages.WaggleError`
with ``bad_request``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from ovos_bus_client.message import Message

from waggle.messages import ErrorCode, RINGER_MODES, VOLUME_STEPS, VOLUME_STREAMS, bad_request
from waggle.queries import PHONE_TYPES

REQUEST = "waggle:request"
REQUEST_RESPONSE = "waggle:request.response"

ALARM_SET = "alarm.set"
TIMER_SET = "timer.set"
ALARMS_SHOW = "alarms.show"
CALENDAR_NEXT = "calendar.next"
APP_OPEN = "app.open"
CONTACT_CALL = "contact.call"
MESSAGE_COMPOSE = "message.compose"
TIMERS_STOP = "timers.stop"
ALARM_DISMISS = "alarm.dismiss"
ALARM_SNOOZE = "alarm.snooze"
NAVIGATE_TO = "navigate.to"
PHONE_STATUS = "phone.status"
TORCH_SET = "torch.set"
VOLUME_SET = "volume.set"
RINGER_SET = "ringer.set"
DND_SET = "dnd.set"

REQUEST_NAMES = (ALARM_SET, TIMER_SET, ALARMS_SHOW, CALENDAR_NEXT, APP_OPEN, CONTACT_CALL,
                 MESSAGE_COMPOSE, TIMERS_STOP, ALARM_DISMISS, ALARM_SNOOZE, NAVIGATE_TO,
                 PHONE_STATUS, TORCH_SET, VOLUME_SET, RINGER_SET, DND_SET)
# The settings requests, each run by one waggle.command.
SETTINGS_REQUESTS = (TORCH_SET, VOLUME_SET, RINGER_SET, DND_SET)

# Which alarms alarm.dismiss picks (AlarmClock's search modes).
DISMISS_MODES = ("time", "next", "label", "all")
# What phone.status reports on.
STATUS_PARTS = ("battery", "network", "sound", "all")

# No response from the phone, or the phone isn't connected; not a WAGGLE.md code.
UNREACHABLE = "unreachable"
RESPONSE_ERRORS = frozenset({c.value for c in ErrorCode} | {UNREACHABLE})


# --- params ------------------------------------------------------------------

def _int(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _one_of(choices: tuple) -> Callable[[Any], bool]:
    return lambda v: v in choices


def _is_iso_date(v: Any) -> bool:
    if not isinstance(v, str) or len(v) != 10:
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


# Per request: param name -> (required, check, description for errors).
_PARAMS: dict[str, dict[str, tuple[bool, Callable[[Any], bool], str]]] = {
    ALARM_SET: {"hour": (True, _int(0, 23), "an integer 0–23"),
                "minute": (True, _int(0, 59), "an integer 0–59"),
                "label": (False, _is_str, "a string")},
    TIMER_SET: {"seconds": (True, _int(1, 86400), "an integer 1–86400"),
                "label": (False, _is_str, "a string")},
    ALARMS_SHOW: {},
    CALENDAR_NEXT: {"count": (False, _int(1, 10), "an integer 1–10"),
                    "day": (False, _is_iso_date, "a date, YYYY-MM-DD")},
    APP_OPEN: {"name": (True, _is_text, "a non-empty string")},
    CONTACT_CALL: {"name": (True, _is_text, "a non-empty string"),
                   "type": (False, lambda v: v in PHONE_TYPES, "cell, home, work or other")},
    MESSAGE_COMPOSE: {"name": (True, _is_text, "a non-empty string"),
                      "body": (True, _is_text, "a non-empty string")},
    TIMERS_STOP: {},
    # "time" needs hour and minute, "label" a label; see _check_dismiss.
    ALARM_DISMISS: {"mode": (False, lambda v: v in DISMISS_MODES, "time, next, label or all"),
                    "hour": (False, _int(0, 23), "an integer 0–23"),
                    "minute": (False, _int(0, 59), "an integer 0–59"),
                    "label": (False, _is_text, "a non-empty string")},
    ALARM_SNOOZE: {"minutes": (False, _int(1, 60), "an integer 1–60")},
    NAVIGATE_TO: {"destination": (True, _is_text, "a non-empty string")},
    PHONE_STATUS: {"about": (False, _one_of(STATUS_PARTS), "battery, network, sound or all")},
    TORCH_SET: {"on": (True, _is_bool, "a boolean")},
    # One of level and step; see _check_volume.
    VOLUME_SET: {"stream": (False, _one_of(VOLUME_STREAMS), "media, ring, alarm or call"),
                 "level": (False, _int(0, 100), "an integer 0–100"),
                 "step": (False, _one_of(VOLUME_STEPS), "up or down")},
    RINGER_SET: {"mode": (True, _one_of(RINGER_MODES), "normal, vibrate or silent")},
    DND_SET: {"on": (True, _is_bool, "a boolean")},
}


def _check_volume(params: dict) -> None:
    """The stream defaults to media; exactly one of level and step."""
    params.setdefault("stream", "media")
    if ("level" in params) == ("step" in params):
        raise bad_request("volume.set needs exactly one of 'level' and 'step'")


def _check_dismiss(params: dict) -> None:
    """The mode defaults to "next"; "time" needs hour and minute, "label" a label."""
    mode = params.setdefault("mode", "next")
    if mode == "time" and not ("hour" in params and "minute" in params):
        raise bad_request("alarm.dismiss mode 'time' needs 'hour' and 'minute'")
    if mode == "label" and "label" not in params:
        raise bad_request("alarm.dismiss mode 'label' needs 'label'")
    for name in ("hour", "minute", "label"):
        wanted = mode == "time" if name != "label" else mode == "label"
        if not wanted:
            params.pop(name, None)


def _check_params(request: str, params: Any) -> dict:
    """The known params of ``request``, validated; unknown ones are dropped."""
    if not isinstance(params, dict):
        raise bad_request("'params' must be an object")
    known = {}
    for name, (required, check, what) in _PARAMS[request].items():
        value = params.get(name)
        if value is None:
            if required:
                raise bad_request(f"{request} needs param {name!r}")
            continue
        if not check(value):
            raise bad_request(f"{request} param {name!r} must be {what}, got {value!r}")
        known[name] = value
    return known


# --- request -----------------------------------------------------------------

@dataclass(frozen=True)
class WaggleRequest:
    """The data of a ``waggle:request``. ``params`` keeps only the known params."""
    request: str
    params: dict = field(default_factory=dict)
    speak: bool = True

    def __post_init__(self):
        if self.request not in _PARAMS:
            raise bad_request(f"unknown request {self.request!r}")
        if not isinstance(self.speak, bool):
            raise bad_request("'speak' must be a boolean")
        object.__setattr__(self, "params", _check_params(self.request, self.params))
        if self.request == ALARM_DISMISS:
            _check_dismiss(self.params)
        if self.request == VOLUME_SET:
            _check_volume(self.params)

    @classmethod
    def from_dict(cls, d: Any) -> "WaggleRequest":
        if not isinstance(d, dict):
            raise bad_request("request must be an object")
        params = d.get("params")
        return cls(request=d.get("request"), params={} if params is None else params,
                   speak=d.get("speak", True))

    def to_dict(self) -> dict:
        return {"request": self.request, "params": dict(self.params), "speak": self.speak}


# --- response ----------------------------------------------------------------

@dataclass(frozen=True)
class RequestResponse:
    """The data of a ``waggle:request.response``.

    ``error`` is set iff ``ok`` is false: a WAGGLE.md error code or
    :data:`UNREACHABLE`, stored as its string value. ``summary`` says what
    happened in words.
    """
    ok: bool
    summary: str
    error: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.ok, bool):
            raise bad_request("'ok' must be a boolean")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise bad_request("'summary' must be a non-empty string")
        if self.ok == (self.error is not None):
            raise bad_request("'error' must be set if and only if 'ok' is false")
        if self.error is not None:
            error = self.error.value if isinstance(self.error, ErrorCode) else self.error
            if error not in RESPONSE_ERRORS:
                raise bad_request(f"bad error {self.error!r}")
            object.__setattr__(self, "error", error)

    @classmethod
    def success(cls, summary: str) -> "RequestResponse":
        return cls(ok=True, summary=summary)

    @classmethod
    def failure(cls, error: ErrorCode | str, summary: str) -> "RequestResponse":
        return cls(ok=False, summary=summary, error=error)

    @classmethod
    def from_dict(cls, d: Any) -> "RequestResponse":
        if not isinstance(d, dict):
            raise bad_request("request response must be an object")
        return cls(ok=d.get("ok"), summary=d.get("summary"), error=d.get("error"))

    def to_dict(self) -> dict:
        d = {"ok": self.ok, "summary": self.summary}
        if self.error is not None:
            d["error"] = self.error
        return d


# --- bus messages ------------------------------------------------------------

def request_message(request: WaggleRequest, context: Optional[dict] = None) -> Message:
    """A ``waggle:request``; pass the utterance's context so replies reach the same peer."""
    return Message(REQUEST, request.to_dict(), dict(context or {}))


def response_message(origin: Message, response: RequestResponse) -> Message:
    """The ``waggle:request.response`` for ``origin``, as a reply to it."""
    return origin.reply(REQUEST_RESPONSE, response.to_dict())

