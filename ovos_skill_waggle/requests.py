"""The ``waggle:request`` and ``waggle:request.response`` shapes (see SPEC.md "Requests").

A request is a structured ask, such as ``timer.set {seconds: 600}``, from the
pipeline stage, the persona's tool calls or another skill. The handler that
runs it answers with a :class:`RequestResponse` as a reply to the request.
Malformed requests and responses raise :class:`~waggle.messages.WaggleError`
with ``bad_request``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ovos_bus_client.message import Message

from waggle.messages import ErrorCode, bad_request

REQUEST = "waggle:request"
REQUEST_RESPONSE = "waggle:request.response"

ALARM_SET = "alarm.set"
TIMER_SET = "timer.set"
ALARMS_SHOW = "alarms.show"
CALENDAR_NEXT = "calendar.next"
APP_OPEN = "app.open"
CONTACT_CALL = "contact.call"
MESSAGE_COMPOSE = "message.compose"

REQUEST_NAMES = (ALARM_SET, TIMER_SET, ALARMS_SHOW, CALENDAR_NEXT, APP_OPEN, CONTACT_CALL,
                 MESSAGE_COMPOSE)

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


# Per request: param name -> (required, check, description for errors).
_PARAMS: dict[str, dict[str, tuple[bool, Callable[[Any], bool], str]]] = {
    ALARM_SET: {"hour": (True, _int(0, 23), "an integer 0–23"),
                "minute": (True, _int(0, 59), "an integer 0–59"),
                "label": (False, _is_str, "a string")},
    TIMER_SET: {"seconds": (True, _int(1, 86400), "an integer 1–86400"),
                "label": (False, _is_str, "a string")},
    ALARMS_SHOW: {},
    CALENDAR_NEXT: {"count": (False, _int(1, 10), "an integer 1–10")},
    APP_OPEN: {"name": (True, _is_text, "a non-empty string")},
    CONTACT_CALL: {"name": (True, _is_text, "a non-empty string")},
    MESSAGE_COMPOSE: {"name": (True, _is_text, "a non-empty string"),
                      "body": (True, _is_text, "a non-empty string")},
}


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

