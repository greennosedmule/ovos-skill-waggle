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

``navigate.to`` is an intent-only request too. ``phone.status`` is a query
only. The settings requests (``torch.set``, ``volume.set``, ``ringer.set``,
``dnd.set``) each send one ``waggle.command``, which the phone runs itself;
they're allowed when the phone announced the command and its rules allow it.

``timers.stop`` sends two intents, ``DISMISS_TIMER`` (a ringing timer) and then
``SHOW_TIMERS`` (so the user can cancel a running one: no intent can), and is
allowed when either is. Dismissing comes first: it shows nothing, while the
Clock that ``SHOW_TIMERS`` opens covers the phone app, which then can't launch.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from waggle import commands, intents
from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT as Q_CALENDAR_NEXT, CONTACTS_LOOKUP, PHONE_STATUS as Q_PHONE_STATUS,
    Capabilities, Command, Intent, new_request_id,
)
from waggle.rules import decide_command, decide_for, query_enabled

from ovos_skill_waggle.requests import (
    ALARM_DISMISS, ALARM_SET, ALARM_SNOOZE, ALARMS_SHOW, APP_OPEN, CALENDAR_NEXT, CONTACT_CALL,
    DND_SET, MESSAGE_COMPOSE, NAVIGATE_TO, PHONE_STATUS, RINGER_SET, TIMER_SET, TIMERS_STOP,
    TORCH_SET, VOLUME_SET, WaggleRequest,
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


_SEARCH_MODES = {"time": intents.SEARCH_TIME, "next": intents.SEARCH_NEXT,
                 "label": intents.SEARCH_LABEL, "all": intents.SEARCH_ALL}


def _alarm_dismiss(request: WaggleRequest, settings: Settings) -> Intent:
    p = request.params
    return intents.dismiss_alarm(_SEARCH_MODES[p["mode"]], hour=p.get("hour"),
                                 minute=p.get("minute"), label=p.get("label"),
                                 package=settings.package(ALARM_DISMISS))


def _alarm_snooze(request: WaggleRequest, settings: Settings) -> Intent:
    return intents.snooze_alarm(request.params.get("minutes"),
                                package=settings.package(ALARM_SNOOZE))


def _navigate_to(request: WaggleRequest, settings: Settings) -> Intent:
    return intents.navigate(request.params["destination"], package=settings.package(NAVIGATE_TO))


INTENT_BUILDERS: dict[str, Callable[[WaggleRequest, Settings], Intent]] = {
    ALARM_SET: _alarm_set,
    TIMER_SET: _timer_set,
    ALARMS_SHOW: _alarms_show,
    ALARM_DISMISS: _alarm_dismiss,
    ALARM_SNOOZE: _alarm_snooze,
    NAVIGATE_TO: _navigate_to,
}


def _volume(request: WaggleRequest) -> Command:
    p = request.params
    return commands.volume(p["stream"], level=p.get("level"), step=p.get("step"))


COMMAND_BUILDERS: dict[str, Callable[[WaggleRequest], Command]] = {
    TORCH_SET: lambda r: commands.torch(r.params["on"]),
    VOLUME_SET: _volume,
    RINGER_SET: lambda r: commands.ringer(r.params["mode"]),
    DND_SET: lambda r: commands.dnd(r.params["on"]),
}

# The requests this release runs.
SUPPORTED_REQUESTS = (frozenset(INTENT_BUILDERS) | frozenset(COMMAND_BUILDERS)
                      | {CALENDAR_NEXT, APP_OPEN, CONTACT_CALL, MESSAGE_COMPOSE, TIMERS_STOP,
                         PHONE_STATUS})


def timers_stop_intents(settings: Settings) -> list[Intent]:
    """``timers.stop``'s intents, in the order they're sent: silence, then show."""
    package = settings.package(TIMERS_STOP)
    return [intents.dismiss_timer(package=package), intents.show_timers(package=package)]


def build_intent(request: WaggleRequest, settings: Settings) -> Intent:
    """The intent for an intent-only request, with a new request id.

    Raises ``KeyError`` for a request that isn't intent-only and ``ValueError``
    for params the builder rejects.
    """
    return INTENT_BUILDERS[request.request](request, settings)


def build_command(request: WaggleRequest) -> Command:
    """The command for a settings request, with a new request id.

    Raises ``KeyError`` for a request that isn't one.
    """
    return COMMAND_BUILDERS[request.request](request)


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
    if request.request == PHONE_STATUS:
        return Q_PHONE_STATUS
    if request.request == APP_OPEN:
        return APPS_LIST
    if request.request in (CONTACT_CALL, MESSAGE_COMPOSE):
        # A number said aloud needs no lookup.
        return None if spoken_number(request.params["name"]) else CONTACTS_LOOKUP
    return None


def precheck_intent(request: WaggleRequest, settings: Settings) -> Optional[Intent]:
    """The intent the phone's rules judge before any lookup, or None for a query-only or
    command request.

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
    if request.request == TIMERS_STOP:
        return any(decide_for(capabilities, i).allowed for i in timers_stop_intents(settings))
    if request.request in COMMAND_BUILDERS:
        return decide_command(capabilities, build_command(request)).allowed
    try:
        intent = precheck_intent(request, settings)
    except ValueError:
        return False
    return intent is None or decide_for(capabilities, intent).allowed
