"""Android intent builders for the hub (see SPEC.md "Requests").

Each builder returns a validated :class:`~waggle.messages.Intent` with a new
request id (unless ``id`` is given), a short human ``description`` and an
optional ``package`` override. Bad arguments are programmer errors on the hub
and raise ``ValueError``. Times are wall-clock numbers the caller already
computed in the phone's timezone.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from waggle.messages import Extra, Intent, new_request_id

# android.content.Intent
ACTION_MAIN = "android.intent.action.MAIN"
ACTION_DIAL = "android.intent.action.DIAL"
ACTION_SENDTO = "android.intent.action.SENDTO"
CATEGORY_LAUNCHER = "android.intent.category.LAUNCHER"

# android.provider.AlarmClock
ACTION_SET_ALARM = "android.intent.action.SET_ALARM"
ACTION_SET_TIMER = "android.intent.action.SET_TIMER"
ACTION_SHOW_ALARMS = "android.intent.action.SHOW_ALARMS"
EXTRA_HOUR = "android.intent.extra.alarm.HOUR"
EXTRA_MINUTES = "android.intent.extra.alarm.MINUTES"
EXTRA_MESSAGE = "android.intent.extra.alarm.MESSAGE"
EXTRA_SKIP_UI = "android.intent.extra.alarm.SKIP_UI"
EXTRA_LENGTH = "android.intent.extra.alarm.LENGTH"

# The SMS body extra the stock messaging apps read with ACTION_SENDTO.
EXTRA_SMS_BODY = "sms_body"

SCHEME_TEL = "tel"
SCHEME_SMSTO = "smsto"

TIMER_MIN_S, TIMER_MAX_S = 1, 86400  # AlarmClock.EXTRA_LENGTH's range

# Kept literal in tel:/smsto: URIs: the leading "+" and RFC 3966's visual
# separators. Everything else, "#" above all, is percent-encoded.
_NUMBER_SAFE = "+-.()*"


# --- argument checks ---------------------------------------------------------

def _int(value: Any, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer {lo}–{hi}, got {value!r}")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool, got {value!r}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _label(value: Any) -> Optional[str]:
    """An optional label; an empty one counts as none."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"label must be a string, got {value!r}")
    return value or None


def _optional_text(value: Any, name: str) -> Optional[str]:
    return None if value is None else _text(value, name)


def _number_uri(scheme: str, number: Any) -> str:
    number = _text(number, "number").strip()
    return f"{scheme}:{quote(number, safe=_NUMBER_SAFE)}"


def _intent(action: str, description: str, id: Optional[str], package: Optional[str],
            **fields: Any) -> Intent:
    return Intent(id=new_request_id() if id is None else id, action=action,
                  description=description, package=_optional_text(package, "package"), **fields)


# --- descriptions ------------------------------------------------------------

def _clock(hour: int, minute: int) -> str:
    """12-hour clock text: 0:05 -> "12:05 AM", 18:30 -> "6:30 PM"."""
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _duration(seconds: int) -> str:
    """'10-minute timer' for a single unit, 'a timer for 1 hour and 30 minutes' otherwise."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [(n, unit) for n, unit in ((hours, "hour"), (minutes, "minute"), (secs, "second")) if n]
    if len(parts) == 1:
        n, unit = parts[0]
        # n is at most 59 here, where only 8, 11 and 18 take "an".
        return f"{'an' if n in (8, 11, 18) else 'a'} {n}-{unit} timer"
    words = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts]
    return f"a timer for {', '.join(words[:-1])} and {words[-1]}"


# --- builders ----------------------------------------------------------------

def set_alarm(hour: int, minute: int, *, label: Optional[str] = None, skip_ui: bool = True,
              package: Optional[str] = None, id: Optional[str] = None) -> Intent:
    """``SET_ALARM`` for ``hour``:``minute`` on the phone's wall clock."""
    hour, minute = _int(hour, "hour", 0, 23), _int(minute, "minute", 0, 59)
    label = _label(label)
    extras = {EXTRA_HOUR: Extra("int", hour), EXTRA_MINUTES: Extra("int", minute),
              EXTRA_SKIP_UI: Extra("bool", _bool(skip_ui, "skip_ui"))}
    description = f"Set an alarm for {_clock(hour, minute)}"
    if label:
        extras[EXTRA_MESSAGE] = Extra("string", label)
        description += f' labeled "{label}"'
    return _intent(ACTION_SET_ALARM, description, id, package, extras=extras)


def set_timer(seconds: int, *, label: Optional[str] = None, skip_ui: bool = True,
              package: Optional[str] = None, id: Optional[str] = None) -> Intent:
    """``SET_TIMER`` for ``seconds`` (1–86400)."""
    seconds = _int(seconds, "seconds", TIMER_MIN_S, TIMER_MAX_S)
    label = _label(label)
    extras = {EXTRA_LENGTH: Extra("int", seconds),
              EXTRA_SKIP_UI: Extra("bool", _bool(skip_ui, "skip_ui"))}
    description = f"Start {_duration(seconds)}"
    if label:
        extras[EXTRA_MESSAGE] = Extra("string", label)
        description += f' labeled "{label}"'
    return _intent(ACTION_SET_TIMER, description, id, package, extras=extras)


def show_alarms(*, package: Optional[str] = None, id: Optional[str] = None) -> Intent:
    return _intent(ACTION_SHOW_ALARMS, "Show alarms", id, package)


def launch_app(package: str, *, label: Optional[str] = None, id: Optional[str] = None) -> Intent:
    """``MAIN`` + ``LAUNCHER`` restricted to ``package``; ``label`` is the app's name."""
    package = _text(package, "package")
    label = _optional_text(label, "label")
    return _intent(ACTION_MAIN, f"Open {label or package}", id, package,
                   categories=(CATEGORY_LAUNCHER,))


def dial(number: str, *, name: Optional[str] = None, package: Optional[str] = None,
         id: Optional[str] = None) -> Intent:
    """``DIAL`` with a ``tel:`` URI; the user still taps call."""
    uri = _number_uri(SCHEME_TEL, number)
    who = _optional_text(name, "name") or number.strip()
    return _intent(ACTION_DIAL, f"Call {who}", id, package, data=uri)


def compose_sms(number: str, body: str, *, name: Optional[str] = None,
                package: Optional[str] = None, id: Optional[str] = None) -> Intent:
    """``SENDTO`` with an ``smsto:`` URI and the body prefilled; the user still taps send."""
    uri = _number_uri(SCHEME_SMSTO, number)
    body = _text(body, "body")
    who = _optional_text(name, "name") or number.strip()
    return _intent(ACTION_SENDTO, f"Text {who}", id, package, data=uri,
                   extras={EXTRA_SMS_BODY: Extra("string", body)})
