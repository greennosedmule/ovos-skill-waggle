"""What each request needs from the phone (see SPEC.md "Requests").

The pipeline stage and the handlers both decide here whether the phone would
allow a request, so the stage's pre-check judges what the handler would send.
Intent-only requests (``alarm.set``, ``timer.set``, ``alarms.show``) are built
whole. The lookup requests (``app.open``, ``contact.call``,
``message.compose``) build their intent from a query result, so before the
lookup they're judged by a stand-in intent with the same action, scheme and
category: a rule never looks at the number or the extras. A rule naming a
package can still block the real intent; the handler checks that again once
it has one. ``calendar.next`` is a query only.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from waggle import intents
from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT as Q_CALENDAR_NEXT, CONTACTS_LOOKUP, Capabilities, Intent,
    new_request_id,
)
from waggle.rules import decide_for, query_enabled

from ovos_skill_waggle.requests import (
    ALARM_SET, ALARMS_SHOW, APP_OPEN, CALENDAR_NEXT, CONTACT_CALL, MESSAGE_COMPOSE, TIMER_SET,
    WaggleRequest,
)
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
SUPPORTED_REQUESTS = frozenset(INTENT_BUILDERS) | {CALENDAR_NEXT, APP_OPEN, CONTACT_CALL,
                                                   MESSAGE_COMPOSE}


def build_intent(request: WaggleRequest, settings: Settings) -> Intent:
    """The intent for an intent-only request, with a new request id.

    Raises ``KeyError`` for a request that isn't intent-only and ``ValueError``
    for params the builder rejects.
    """
    return INTENT_BUILDERS[request.request](request, settings)


# --- phone numbers said aloud ------------------------------------------------

_NUMBER = re.compile(r"\+?[\d\s().-]+")


def spoken_number(name: str) -> Optional[str]:
    """``name`` as a phone number if it is one ("555 1234", "+1 555-123-4567"), else None."""
    name = name.strip()
    if not _NUMBER.fullmatch(name):
        return None
    digits = re.sub(r"\D", "", name)
    if len(digits) < 3:
        return None
    return ("+" if name.startswith("+") else "") + digits


# --- what a request needs ----------------------------------------------------

def required_query(request: WaggleRequest) -> Optional[str]:
    """The Waggle query a request runs on the phone, or None."""
    if request.request == CALENDAR_NEXT:
        return Q_CALENDAR_NEXT
    if request.request == APP_OPEN:
        return APPS_LIST
    if request.request in (CONTACT_CALL, MESSAGE_COMPOSE):
        # A number said aloud needs no lookup.
        return None if spoken_number(request.params["name"]) else CONTACTS_LOOKUP
    return None


def precheck_intent(request: WaggleRequest, settings: Settings) -> Optional[Intent]:
    """The intent the phone's rules judge before any lookup, or None for a query-only request.

    Raises ``ValueError`` for params an intent builder rejects.
    """
    if request.request in INTENT_BUILDERS:
        return build_intent(request, settings)
    package = settings.package(request.request)
    if request.request == APP_OPEN:
        return Intent(id=new_request_id(), action=intents.ACTION_MAIN,
                      categories=(intents.CATEGORY_LAUNCHER,))
    if request.request == CONTACT_CALL:
        return intents.dial("0", package=package)
    if request.request == MESSAGE_COMPOSE:
        return intents.compose_sms("0", "-", package=package)
    return None


def phone_allows(capabilities: Capabilities, request: WaggleRequest, settings: Settings) -> bool:
    """Whether the phone, by its announced rules and queries, would run ``request``.

    An "ask" rule allows. Disabled or unsupported requests are never allowed.
    """
    if not settings.enabled(request.request) or request.request not in SUPPORTED_REQUESTS:
        return False
    query = required_query(request)
    if query is not None and not query_enabled(capabilities, query):
        return False
    try:
        intent = precheck_intent(request, settings)
    except ValueError:
        return False
    return intent is None or decide_for(capabilities, intent).allowed
