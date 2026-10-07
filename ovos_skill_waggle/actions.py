"""The Android intent behind each intent-only request (see SPEC.md "Requests").

The pipeline stage and the handlers both build a request's intent here, so the
stage's rule pre-check judges exactly the intent the handler would send.
Requests that need a lookup on the phone first (``app.open``,
``contact.call``, ``message.compose``) and the query requests arrive in S3.
"""
from __future__ import annotations

from typing import Callable

from waggle import intents
from waggle.messages import Intent

from ovos_skill_waggle.requests import ALARM_SET, ALARMS_SHOW, TIMER_SET, WaggleRequest
from ovos_skill_waggle.settings import Settings


def _alarm_set(request: WaggleRequest, settings: Settings) -> Intent:
    p = request.params
    return intents.set_alarm(p["hour"], p["minute"], label=p.get("label"),
                             package=settings.package(ALARM_SET))


def _timer_set(request: WaggleRequest, settings: Settings) -> Intent:
    p = request.params
    return intents.set_timer(p["seconds"], label=p.get("label"),
                             package=settings.package(TIMER_SET))


def _alarms_show(request: WaggleRequest, settings: Settings) -> Intent:
    return intents.show_alarms(package=settings.package(ALARMS_SHOW))


INTENT_BUILDERS: dict[str, Callable[[WaggleRequest, Settings], Intent]] = {
    ALARM_SET: _alarm_set,
    TIMER_SET: _timer_set,
    ALARMS_SHOW: _alarms_show,
}

# The requests this release runs.
SUPPORTED_REQUESTS = frozenset(INTENT_BUILDERS)


def build_intent(request: WaggleRequest, settings: Settings) -> Intent:
    """The intent for an intent-only request, with a new request id.

    Raises ``KeyError`` for a request that isn't intent-only (or not yet
    supported) and ``ValueError`` for params the builder rejects.
    """
    return INTENT_BUILDERS[request.request](request, settings)
