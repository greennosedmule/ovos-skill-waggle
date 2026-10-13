"""Stopping timers, dismissing alarms and snoozing: parsing, intents, handlers, stage and tools.

Android's Clock can silence ringing timers and show the timers (no intent
cancels a running one), skip an alarm's next ring, and snooze a ringing alarm.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.parsing import parse_utterance
from ovos_skill_waggle.requests import (
    ALARM_DISMISS, ALARM_SNOOZE, REQUEST, REQUEST_RESPONSE, TIMERS_STOP, WaggleRequest,
)
from ovos_skill_waggle.tools import Outcome, WaggleToolBox
from waggle import intents
from waggle.fake_phone import AskAnswer
from waggle.intents import (
    ACTION_DISMISS_ALARM, ACTION_DISMISS_TIMER, ACTION_SET_TIMER, ACTION_SHOW_TIMERS,
    ACTION_SNOOZE_ALARM, EXTRA_ALARM_SEARCH_MODE, EXTRA_ALARM_SNOOZE_DURATION, EXTRA_HOUR,
    EXTRA_MESSAGE, EXTRA_MINUTES,
)
from waggle.messages import INTENT, ErrorCode, WaggleError

from conftest import NOW, OTHER_PEER, PEER, capabilities, match_message, utterance
from test_tools import as_persona, offered, persona_query

# 14:10 on the fake phone's clock, as the stage reads times.
AFTERNOON = NOW.astimezone(ZoneInfo("America/Chicago"))


def parsed(text):
    p = parse_utterance(text, "en-US", AFTERNOON)
    if p is None:
        return None
    return (p.request, p.params, p.missing) if p.missing else (p.request, p.params)


def dismiss(mode, missing=None, **params):
    return (ALARM_DISMISS, {"mode": mode, **params}) + ((missing,) if missing else ())


# --- parsing -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "stop the timer", "cancel my timer", "cancel the pasta timer", "cancel the 10 minute timer",
    "cancel all timers", "cancel the timer for the pasta", "turn off the timer",
    "turn the timer off", "silence the timer", "stop the timer that's ringing",
    "please stop the countdown", "Dismiss the timer.",
])
def test_timer_stopping(text):
    assert parsed(text) == (TIMERS_STOP, {})


@pytest.mark.parametrize("text,expected", [
    ("cancel my alarm", dismiss("next")),
    ("turn off the alarm", dismiss("next")),
    ("turn my alarm off", dismiss("next")),
    ("stop the alarm", dismiss("next")),          # a ringing one comes first on the phone
    ("dismiss my next alarm", dismiss("next")),
    ("turn off tomorrow's alarm", dismiss("next")),
    ("cancel my alarm for tomorrow", dismiss("next")),
    ("turn off my 6:30 am alarm", dismiss("time", hour=6, minute=30)),
    ("cancel my 7 am alarm", dismiss("time", hour=7, minute=0)),
    ("turn off my 18:30 alarm", dismiss("time", hour=18, minute=30)),
    ("cancel my 7 alarm tomorrow morning", dismiss("time", hour=7, minute=0)),
    ("cancel my 9 alarm tomorrow night", dismiss("time", hour=21, minute=0)),
    # No a.m. or p.m.: asked, since a wrong guess dismisses nothing or the wrong alarm.
    ("cancel my 6:30 alarm", dismiss("time", "half", hour=6, minute=30)),
    ("cancel the alarm at 7", dismiss("time", "half", hour=7, minute=0)),
    ("cancel my 7 alarm", dismiss("time", "half", hour=7, minute=0)),
    # A label: its time is asked for (labels match case-sensitively, and one in two alarms
    # opens the Clock's picker, which crashes on Android 16).
    ("cancel my work alarm", dismiss("time", "alarm_time", label="work")),
    ("cancel the alarm called gym", dismiss("time", "alarm_time", label="gym")),
    ("cancel all my alarms", dismiss("all")),
    ("cancel my alarms", dismiss("all")),
    ("delete all alarms", dismiss("all")),
])
def test_alarm_dismissing(text, expected):
    assert parsed(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("snooze", (ALARM_SNOOZE, {})),
    ("snooze the alarm", (ALARM_SNOOZE, {})),
    ("hit snooze", (ALARM_SNOOZE, {})),
    ("snooze for 10 minutes", (ALARM_SNOOZE, {"minutes": 10})),
    ("snooze it for five minutes", (ALARM_SNOOZE, {"minutes": 5})),
])
def test_snoozing(text, expected):
    assert parsed(text) == expected


@pytest.mark.parametrize("text", [
    "stop", "cancel",                        # OVOS's stop stage
    "pause the timer", "how long is left on my timer",
    "cancel my monday alarm",                # another day: the Clock can't
    "cancel my alarm on friday",
    "cancel the smoke alarm", "turn off the car alarm",
    "snooze for 2 hours",
    "stop the timer and set a new one for 5 minutes",  # compound: the persona's
])
def test_not_taken(text):
    assert parsed(text) is None


def test_setting_is_unchanged():
    assert parsed("set a timer for 10 minutes") == ("timer.set", {"seconds": 600})
    assert parsed("what time is my alarm set for") == ("alarms.show", {})


# --- requests and intents ------------------------------------------------------

def test_dismiss_request_modes():
    assert WaggleRequest(ALARM_DISMISS, {}).params == {"mode": "next"}
    assert WaggleRequest(ALARM_DISMISS, {"mode": "all", "hour": 7}).params == {"mode": "all"}
    for params in ({"mode": "time", "hour": 7}, {"mode": "label"}, {"mode": "soon"}):
        with pytest.raises(WaggleError):
            WaggleRequest(ALARM_DISMISS, params)
    with pytest.raises(WaggleError):
        WaggleRequest(ALARM_SNOOZE, {"minutes": 61})


def values(intent):
    return {k: e.value for k, e in intent.extras.items()}


def test_intents():
    assert intents.dismiss_timer().action == ACTION_DISMISS_TIMER
    assert intents.show_timers().action == ACTION_SHOW_TIMERS
    t = intents.dismiss_alarm(intents.SEARCH_TIME, hour=6, minute=30)
    assert t.action == ACTION_DISMISS_ALARM
    assert values(t) == {EXTRA_ALARM_SEARCH_MODE: "android.time", EXTRA_HOUR: 6,
                         EXTRA_MINUTES: 30}
    assert t.description == "Dismiss the 6:30 AM alarm"
    assert values(intents.dismiss_alarm(intents.SEARCH_LABEL, label="Work")) == {
        EXTRA_ALARM_SEARCH_MODE: "android.label", EXTRA_MESSAGE: "Work"}
    assert values(intents.dismiss_alarm()) == {EXTRA_ALARM_SEARCH_MODE: "android.next"}
    assert values(intents.dismiss_alarm(intents.SEARCH_ALL)) == {
        EXTRA_ALARM_SEARCH_MODE: "android.all"}
    assert values(intents.snooze_alarm()) == {}
    assert values(intents.snooze_alarm(10)) == {EXTRA_ALARM_SNOOZE_DURATION: 10}
    with pytest.raises(ValueError):
        intents.dismiss_alarm("android.soon")
    with pytest.raises(ValueError):
        intents.dismiss_alarm(intents.SEARCH_TIME, hour=6)


# --- handlers ------------------------------------------------------------------

def run(bus, recorder, name, params=None, peer=PEER):
    recorder.clear()
    bus.emit(Message(REQUEST, {"request": name, "params": params or {}, "speak": True},
                     {"source": peer, "peer": peer, "destination": "skills"}))
    return recorder.of(REQUEST_RESPONSE)[0].data


def spoken(recorder):
    return [m.data["utterance"] for m in recorder.of("speak")]


def actions(phone):
    return [e.request.data["action"] for e in phone.log]


def test_timers_stop_silences_then_shows(bus, recorder, phone):
    response = run(bus, recorder, TIMERS_STOP)
    said = "I've stopped any ringing timer. Here are your timers."
    assert response == {"ok": True, "summary": said}
    assert spoken(recorder) == [said]
    assert actions(phone) == [ACTION_DISMISS_TIMER, ACTION_SHOW_TIMERS]


def test_timers_stop_on_a_clock_without_dismiss_still_shows(bus, recorder, pipeline, make_phone):
    phone = make_phone(launch_by_action={ACTION_DISMISS_TIMER: ErrorCode.NO_HANDLER})
    assert run(bus, recorder, TIMERS_STOP) == {"ok": True, "summary": "Here are your timers."}
    assert actions(phone) == [ACTION_DISMISS_TIMER, ACTION_SHOW_TIMERS]


def test_timers_stop_skips_what_the_phone_blocks(bus, recorder, pipeline, make_phone):
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_DISMISS_TIMER, "mode": "run"}]))
    assert run(bus, recorder, TIMERS_STOP)["summary"] == "I've stopped any ringing timer."
    assert actions(phone) == [ACTION_DISMISS_TIMER]


def test_timers_stop_declined_ends_it(bus, recorder, pipeline, make_phone):
    phone = make_phone(ask_answer=AskAnswer.DECLINE, capabilities=capabilities(
        rules=[{"action": ACTION_DISMISS_TIMER, "mode": "ask"},
               {"action": ACTION_SHOW_TIMERS, "mode": "run"}], ask_timeout_s=0.3))
    response = run(bus, recorder, TIMERS_STOP)
    assert response["ok"] is False and response["error"] == "declined"
    assert actions(phone) == [ACTION_DISMISS_TIMER]


def test_timers_stop_fully_blocked(bus, recorder, pipeline, make_phone):
    phone = make_phone(capabilities=capabilities(rules=[]))
    response = run(bus, recorder, TIMERS_STOP)
    assert response["error"] == "blocked" and phone.log == []


@pytest.mark.parametrize("params,said", [
    ({"mode": "next"}, "I've asked your Clock to dismiss your next alarm, if it's due in the next 24 hours."),
    ({"mode": "time", "hour": 6, "minute": 30},
     "I've asked your Clock to dismiss the 6:30 AM alarm, if it's due in the next 24 hours."),
    ({"mode": "label", "label": "work"}, "I've asked your Clock to dismiss the work alarm, if it's due in the next 24 hours."),
])
def test_alarm_dismiss_asks_on_the_phone(bus, recorder, phone, params, said):
    response = run(bus, recorder, ALARM_DISMISS, params)
    assert response == {"ok": True, "summary": said}
    # The default rule is ask: the user confirms on the phone first.
    assert spoken(recorder) == ["Confirm on your phone.", said]
    assert actions(phone) == [ACTION_DISMISS_ALARM]


ALL_SAID = "I've asked your Clock to dismiss every alarm due in the next 24 hours."


def test_dismiss_all_reads_back_before_the_phone_asks(bus, recorder, phone):
    response = run(bus, recorder, ALARM_DISMISS, {"mode": "all"})
    assert response == {"ok": True, "summary": ALL_SAID}
    assert spoken(recorder) == [
        "That dismisses every alarm due in the next 24 hours. Confirm on your phone.", ALL_SAID]


@pytest.mark.parametrize("answers,sent", [(["yes"], True), (["no"], False),
                                          (["what?", "go ahead"], True), ([None], False)])
def test_dismiss_all_asks_aloud_when_the_phone_wont(bus, recorder, pipeline, make_phone,
                                                    answers, sent):
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_DISMISS_ALARM, "mode": "run"}]))
    asked = []

    def ask(origin, dialog, data):
        asked.append(dialog)
        return answers[len(asked) - 1]

    pipeline.handlers.ask = ask
    response = run(bus, recorder, ALARM_DISMISS, {"mode": "all"})
    assert asked[0] == "ask_dismiss_all"
    assert response["ok"] is sent
    assert actions(phone) == ([ACTION_DISMISS_ALARM] if sent else [])


def test_alarm_dismiss_declined(bus, recorder, pipeline, make_phone):
    make_phone(ask_answer=AskAnswer.DECLINE)
    response = run(bus, recorder, ALARM_DISMISS)
    assert response["ok"] is False and response["error"] == "declined"


def test_alarm_snooze(bus, recorder, phone):
    response = run(bus, recorder, ALARM_SNOOZE, {"minutes": 10})
    assert response == {"ok": True, "summary": "I've asked your Clock to snooze the alarm."}
    sent = phone.log[0].request.data
    assert sent["action"] == ACTION_SNOOZE_ALARM
    assert sent["extras"][EXTRA_ALARM_SNOOZE_DURATION]["value"] == 10


# --- the stage -----------------------------------------------------------------

def match(pipeline, text, peer=PEER):
    return pipeline.match_high([text], "en-US", utterance(text, peer))


def test_stage_takes_them_from_a_phone_only(pipeline, phone):
    for text, request in (("cancel my timer", TIMERS_STOP), ("snooze", ALARM_SNOOZE),
                          ("turn off my 6:30 am alarm", ALARM_DISMISS)):
        assert match(pipeline, text).match_data["request"] == request
        assert match(pipeline, text, OTHER_PEER) is None


def test_stage_leaves_what_the_phone_blocks(pipeline, make_phone):
    # An install from before these rules existed: unmatched actions are blocked.
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "run"}]))
    for text in ("cancel my timer", "snooze", "cancel my alarm"):
        assert match(pipeline, text) is None


def test_stage_takes_timers_stop_if_either_intent_is_allowed(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SHOW_TIMERS, "mode": "run"}]))
    assert match(pipeline, "cancel my timer") is not None


# --- the persona's tools -------------------------------------------------------

@pytest.fixture
def toolbox():
    return WaggleToolBox()


def test_tools_offered_by_their_rules(toolbox, pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SHOW_TIMERS, "mode": "run"},
                                                {"action": ACTION_DISMISS_ALARM, "mode": "ask"}]))
    tools = offered(toolbox, persona_query())
    assert {"stop_timers", "dismiss_alarm"} <= tools
    assert "snooze_alarm" not in tools


@pytest.mark.parametrize("args,mode_extras", [
    ({}, {EXTRA_ALARM_SEARCH_MODE: "android.next"}),
    ({"hour": 6, "minute": 30}, {EXTRA_ALARM_SEARCH_MODE: "android.time", EXTRA_HOUR: 6,
                                 EXTRA_MINUTES: 30}),
    ({"all": True}, {EXTRA_ALARM_SEARCH_MODE: "android.all"}),
])
def test_dismiss_alarm_tool(toolbox, pipeline, make_phone, args, mode_extras):
    phone = make_phone()
    result = as_persona(persona_query(), lambda: toolbox.call_tool("dismiss_alarm", args))
    assert result.ok
    extras = phone.log[0].request.data["extras"]
    assert {k: v["value"] for k, v in extras.items()} == mode_extras


@pytest.mark.parametrize("args", [{"hour": 6}, {"minute": 0}, {"hour": 6, "minute": 0, "all": True}])
def test_dismiss_alarm_tool_refuses_mixed_arguments(toolbox, pipeline, make_phone, args):
    phone = make_phone()
    result = as_persona(persona_query(), lambda: toolbox.call_tool("dismiss_alarm", args))
    assert not result.ok and result.error == "bad_request" and "hour" in result.summary
    assert phone.log == []


def test_stop_and_snooze_tools(toolbox, recorder, pipeline, make_phone):
    phone = make_phone()
    recorder.clear()
    result = as_persona(persona_query(), lambda: toolbox.call_tool("stop_timers", {}))
    assert result == Outcome(ok=True,
                             summary="I've stopped any ringing timer. Here are your timers.")
    assert as_persona(persona_query(), lambda: toolbox.call_tool("snooze_alarm", {})).ok
    assert actions(phone) == [ACTION_DISMISS_TIMER, ACTION_SHOW_TIMERS, ACTION_SNOOZE_ALARM]
    assert recorder.speech() == []  # the persona says it in its own words


def test_no_label_argument(toolbox, pipeline, make_phone):
    make_phone()
    with pytest.raises(ValueError):  # unknown arguments are refused by the tool's schema
        as_persona(persona_query(), lambda: toolbox.call_tool("dismiss_alarm", {"label": "x"}))


# --- follow-up questions -------------------------------------------------------

def dismiss_by_voice(pipeline, recorder, text, answers):
    asked = []

    def ask(origin, dialog, data):
        asked.append(pipeline._render(dialog, data))
        return answers[len(asked) - 1]

    pipeline._ask = ask
    utt = utterance(text)
    m = pipeline.match_high([text], "en-US", utt)
    recorder.clear()
    pipeline.handle_utterance_request(match_message(utt, m))
    return asked


def sent_time(phone):
    extras = phone.log[-1].request.data["extras"]
    return extras[EXTRA_HOUR]["value"], extras[EXTRA_MINUTES]["value"]


def test_bare_hour_asks_which_half(recorder, pipeline, phone):
    asked = dismiss_by_voice(pipeline, recorder, "cancel my 7 alarm", ["the morning one"])
    assert asked == ["The 7:00 AM or the 7:00 PM alarm?"]
    assert sent_time(phone) == (7, 0)


def test_label_asks_for_the_time_then_the_half(recorder, pipeline, phone):
    asked = dismiss_by_voice(pipeline, recorder, "cancel my work alarm", ["6:30", "p.m."])
    assert asked == ["What time is your work alarm set for?",
                     "The 6:30 AM or the 6:30 PM alarm?"]
    assert sent_time(phone) == (18, 30)
    extras = phone.log[-1].request.data["extras"]
    assert extras[EXTRA_ALARM_SEARCH_MODE]["value"] == "android.time"
    assert EXTRA_MESSAGE not in extras


def test_label_answered_with_a_full_time(recorder, pipeline, phone):
    asked = dismiss_by_voice(pipeline, recorder, "cancel the alarm called gym", ["6 am"])
    assert asked == ["What time is your gym alarm set for?"]
    assert sent_time(phone) == (6, 0)


def test_unclear_half_is_asked_again_then_dropped(recorder, pipeline, phone):
    asked = dismiss_by_voice(pipeline, recorder, "cancel my 7 alarm", ["seven", "dunno"])
    assert len(asked) == 2 and phone.log == []


# --- spacing Clock intents -------------------------------------------------------

def test_clock_intents_are_spaced(bus, recorder, pipeline, phone, toolbox):
    """The Clock drops an intent that arrives while a cold start of the last is under way,
    though the phone answers ok; so they go at least clock_gap_s apart, per phone."""
    import time
    pipeline.handlers.clock_gap_s = 0.3
    sent_at = []
    bus.on(INTENT, lambda raw: sent_at.append(time.monotonic()))
    run(bus, recorder, TIMERS_STOP)
    as_persona(persona_query(), lambda: toolbox.call_tool("set_timer", {"seconds": 60}))
    assert len(sent_at) == 3
    assert all(b - a >= 0.29 for a, b in zip(sent_at, sent_at[1:]))


def test_other_requests_are_not_held(bus, recorder, pipeline, make_phone):
    import time
    phone = make_phone(contacts=[{"fn": "Mom", "tel": [{"value": "+15550001111",
                                                         "type": "cell"}]}])
    pipeline.handlers.clock_gap_s = 5.0
    run(bus, recorder, ALARM_SNOOZE)
    start = time.monotonic()
    run(bus, recorder, "contact.call", {"name": "Mom"})
    assert time.monotonic() - start < 1.0
    assert phone.log[-1].request.data["action"].endswith("DIAL")
