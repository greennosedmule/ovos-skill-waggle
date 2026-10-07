"""Utterance to request: timers, alarms, a.m./p.m., relative times and the phone's timezone."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ovos_skill_waggle.parsing import find_duration, normalize, parse_request

CHICAGO = ZoneInfo("America/Chicago")
# 14:10 on the phone's clock, a Wednesday.
AFTERNOON = datetime(2026, 10, 7, 14, 10, tzinfo=CHICAGO)
LATE = datetime(2026, 10, 7, 22, 15, tzinfo=CHICAGO)
EARLY = datetime(2026, 10, 7, 5, 0, tzinfo=CHICAGO)


def parsed(text, now=AFTERNOON, lang="en-US"):
    request = parse_request(text, lang, now)
    if request is None:
        return None
    assert request.speak is True
    return request.request, request.params


# --- timers ------------------------------------------------------------------

TIMERS = [
    ("set a timer for 10 minutes", 600, None),
    ("Set a timer for 10 minutes.", 600, None),
    ("timer for 5 minutes", 300, None),
    ("set a 5 minute timer", 300, None),
    ("start a ten-minute timer", 600, None),
    ("10 minute timer", 600, None),
    ("set a timer for ninety seconds", 90, None),
    ("set a timer for 90 seconds", 90, None),
    ("set a timer for a minute", 60, None),
    ("set a timer for one minute", 60, None),
    ("set a timer for an hour", 3600, None),
    ("set a timer for an hour and a half", 5400, None),
    ("timer for an hour and a half", 5400, None),
    ("set a timer for one and a half hours", 5400, None),
    ("set a timer for 1.5 hours", 5400, None),
    ("set a timer for two and a half minutes", 150, None),
    ("set a timer for 2 minutes and a half", 150, None),
    ("set a timer for half an hour", 1800, None),
    ("set a timer for a half hour", 1800, None),
    ("set a timer for half a minute", 30, None),
    ("set a timer for a quarter of an hour", 900, None),
    ("set a timer for three quarters of an hour", 2700, None),
    ("set a timer for 1 hour and 30 minutes", 5400, None),
    ("set a timer for 2 hours 15 minutes and 10 seconds", 8110, None),
    ("set a timer for twenty five minutes", 1500, None),
    ("set a timer for 10 mins", 600, None),
    ("set a timer for 3 hrs", 10800, None),
    ("set a timer for 45 secs", 45, None),
    ("set a timer for a couple of minutes", 120, None),
    ("set a timer for 24 hours", 86400, None),
    ("countdown 5 minutes", 300, None),
    ("can you set a timer on my phone for 10 minutes please", 600, None),
    ("please start a timer for 3 minutes", 180, None),
    ("set a pasta timer for 8 minutes", 480, "pasta"),
    ("set an egg timer for 6 minutes", 360, "egg"),
    ("set a timer for 20 minutes for the pasta", 1200, "pasta"),
    ("set a timer for 20 minutes for pizza", 1200, "pizza"),
    ("set a timer called tea for 3 minutes", 180, "tea"),
    ("set a 3 minute timer called tea", 180, "tea"),
    ("set a timer for 12 minutes named laundry", 720, "laundry"),
    ("set a kitchen timer for 4 minutes", 240, None),
]


@pytest.mark.parametrize("text,seconds,label", TIMERS)
def test_timer(text, seconds, label):
    params = {"seconds": seconds}
    if label:
        params["label"] = label
    assert parsed(text) == ("timer.set", params)


@pytest.mark.parametrize("text", [
    "set a timer",                       # no length: a follow-up question, S3
    "start a timer",
    "cancel the timer",
    "stop the timer",
    "how much time is left on my timer",
    "how long is left on the pasta timer",
    "pause my timer",
    "what timers do I have",
    "is there a timer running",
    "set a timer for 2 days",            # longer than SET_TIMER's 24 h
    "set a timer for 0 seconds",
    "timer",
    "the timer went off",
])
def test_not_a_timer_request(text):
    assert parsed(text) is None


# --- alarms ------------------------------------------------------------------

ALARMS = [
    # explicit a.m./p.m., in every spelling STT produces
    ("set an alarm for 6:30 am", 6, 30),
    ("set an alarm for 6:30 a.m.", 6, 30),
    ("set an alarm for 6:30 A.M.", 6, 30),
    ("set an alarm for 6:30 a m", 6, 30),
    ("set an alarm for 6:30 P.M.", 18, 30),
    ("set an alarm for 6:30pm", 18, 30),
    ("set an alarm for 7pm", 19, 0),
    ("set an alarm for 7 p.m.", 19, 0),
    ("alarm at 7 p.m.", 19, 0),
    ("set an alarm for 12 am", 0, 0),
    ("set an alarm for 12 pm", 12, 0),
    ("set an alarm for 12:30 a.m.", 0, 30),
    ("set an alarm for seven fifteen pm", 19, 15),
    ("set an alarm for 6 45 am", 6, 45),
    ("set an alarm for eleven forty five at night", 23, 45),
    ("set an alarm for 7 in the morning", 7, 0),
    ("set an alarm for 5 in the evening", 17, 0),
    ("set an alarm for 3 in the afternoon", 15, 0),
    ("set an alarm for 10 tonight", 22, 0),
    ("set an alarm for 8 tomorrow morning", 8, 0),
    # 24-hour forms
    ("set an alarm for 18:30", 18, 30),
    ("set an alarm for 06:30", 6, 30),
    ("set an alarm for 0630", 6, 30),
    ("set an alarm for 1830", 18, 30),
    ("set an alarm for 630 am", 6, 30),
    ("set an alarm for nineteen thirty", 19, 30),
    ("set an alarm for noon", 12, 0),
    ("set an alarm for midday", 12, 0),
    ("set an alarm for midnight", 0, 0),
    # spoken forms
    ("set an alarm for half past six in the morning", 6, 30),
    ("set an alarm for quarter past seven am", 7, 15),
    ("set an alarm for a quarter to seven am", 6, 45),
    ("set an alarm for 10 minutes past 6 am", 6, 10),
    ("set an alarm for 20 to 8 am", 7, 40),
    ("set an alarm for twelve oh five am", 0, 5),
    ("set an alarm for 5 o'clock in the morning", 5, 0),
    ("wake me up at six thirty", 6, 30),
    # "wake me" and "tomorrow" mean morning when a.m./p.m. isn't said
    ("wake me up at 7", 7, 0),
    ("wake me at 6", 6, 0),
    ("wake me up at 7 tomorrow", 7, 0),
    ("set an alarm for tomorrow at 7", 7, 0),
    ("set an alarm for 7 tomorrow", 7, 0),
    # labels
    ("set an alarm for 6:30 am called work", 6, 30),
    # word orders and fillers
    ("6:30 am alarm", 6, 30),
    ("alarm for 7:45 pm", 19, 45),
    ("can you please set an alarm on my phone for 6 am", 6, 0),
    ("I need an alarm at 5:15 am", 5, 15),
    ("set my alarm for 6 am", 6, 0),
    ("set the alarm for 5:50 am", 5, 50),
]


@pytest.mark.parametrize("text,hour,minute", ALARMS)
def test_alarm(text, hour, minute):
    request, params = parsed(text)
    assert request == "alarm.set"
    assert (params["hour"], params["minute"]) == (hour, minute)


def test_alarm_label():
    assert parsed("set an alarm for 6:30 am called work") == \
        ("alarm.set", {"hour": 6, "minute": 30, "label": "work"})
    assert parsed("set an alarm labeled gym for 6 am") == \
        ("alarm.set", {"hour": 6, "minute": 0, "label": "gym"})


@pytest.mark.parametrize("text,now,hour", [
    # a.m./p.m. unsaid: the next time the phone's clock shows it
    ("set an alarm for 6:30", AFTERNOON, 18),   # 14:10 -> this evening
    ("set an alarm for 6:30", LATE, 6),         # 22:15 -> tomorrow morning
    ("set an alarm for 6:30", EARLY, 6),        # 05:00 -> this morning
    ("set an alarm for 9", AFTERNOON, 21),
    ("set an alarm for 2", AFTERNOON, 2),       # 14:10 has passed 2:00 PM -> 2 AM
    ("set an alarm for 2:30", AFTERNOON, 14),   # 2:30 PM is still to come
    ("set an alarm for 12:05", AFTERNOON, 0),
    ("set an alarm for 12:05", EARLY, 12),
    ("set an alarm for 4", LATE, 4),
    # explicit or implied a.m./p.m. ignores the clock
    ("set an alarm for 6:30 pm", EARLY, 18),
    ("wake me up at 7", AFTERNOON, 7),
    ("wake me up at 7", LATE, 7),
    ("set an alarm for 7 tomorrow", AFTERNOON, 7),
])
def test_am_pm_guess(text, now, hour):
    assert parsed(text, now)[1]["hour"] == hour


@pytest.mark.parametrize("text,now,hour,minute", [
    ("set an alarm in 20 minutes", AFTERNOON, 14, 30),
    ("set an alarm for 20 minutes from now", AFTERNOON, 14, 30),
    ("wake me up in 8 hours", LATE, 6, 15),
    ("wake me up in an hour and a half", LATE, 23, 45),
    ("set an alarm in half an hour", LATE, 22, 45),
    ("set an alarm in 2 hours", LATE, 0, 15),  # past midnight on the phone
])
def test_relative_alarm(text, now, hour, minute):
    assert parsed(text, now) == ("alarm.set", {"hour": hour, "minute": minute})


def test_relative_alarm_uses_the_phones_clock():
    # The same instant on two phones in different zones gives each its own wall clock.
    instant = datetime(2026, 10, 7, 19, 10, tzinfo=timezone.utc)
    tokyo = instant.astimezone(ZoneInfo("Asia/Tokyo"))      # 04:10 next day
    chicago = instant.astimezone(CHICAGO)                   # 14:10
    assert parsed("set an alarm in 20 minutes", tokyo)[1] == {"hour": 4, "minute": 30}
    assert parsed("set an alarm in 20 minutes", chicago)[1] == {"hour": 14, "minute": 30}
    # ...and its own a.m./p.m. guess.
    assert parsed("set an alarm for 6:30", tokyo)[1]["hour"] == 6
    assert parsed("set an alarm for 6:30", chicago)[1]["hour"] == 18


@pytest.mark.parametrize("text", [
    "set an alarm",                  # no time: a follow-up question, S3
    "set an alarm for tomorrow",
    "cancel my alarm",
    "delete the 6:30 alarm",
    "turn off the alarm",
    "snooze the alarm",
    "stop the alarm",
    "change my alarm to 7",
    "the fire alarm is beeping at 3",
    "the smoke alarm went off at 5",
    "what time is it",
    "what's the weather tomorrow at 7",
    "remind me to call mom at 6",
    "set an alarm for friday at 7",  # SET_ALARM has no date: other days aren't taken
    "set an alarm for 7 on monday",
    "set an alarm for 6 am every weekday",
    "set an alarm for the day after tomorrow at 7",
    "set an alarm for october 12th at 8",
    "tell me a joke",
    "",
])
def test_not_an_alarm_request(text):
    assert parsed(text) is None


# --- alarms.show -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "show my alarms",
    "show me my alarms",
    "open my alarms",
    "list alarms",
    "what alarms do I have",
    "which alarms are set",
    "do I have any alarms",
    "when is my alarm",
    "when is my next alarm",
    "what time is my alarm set for",
    "what's my next alarm",
    "check my alarms",
])
def test_alarms_show(text):
    assert parsed(text) == ("alarms.show", {})


@pytest.mark.parametrize("text", ["cancel my alarms", "delete all alarms", "turn off my alarms"])
def test_alarms_not_shown(text):
    assert parsed(text) is None


# --- languages and helpers ---------------------------------------------------

@pytest.mark.parametrize("lang", ["de-DE", "fr", "", None])
def test_english_only(lang):
    assert parse_request("set a timer for 10 minutes", lang, AFTERNOON) is None


@pytest.mark.parametrize("lang", ["en-US", "en-us", "en-GB", "en"])
def test_english_variants(lang):
    assert parse_request("set a timer for 10 minutes", lang, AFTERNOON) is not None


def test_normalize():
    assert normalize("Hey, can you set a 10-minute timer?") == "set a 10 minute timer"
    assert normalize("Alarm at 6:30 A.M.") == "alarm at 6:30 am"
    assert normalize("alarm at 7PM please") == "alarm at 7 pm"
    assert normalize("five o'clock") == "five oclock"
    assert normalize("set a timer on my phone") == "set a timer"


def test_find_duration_context():
    d = find_duration("set a timer for 10 minutes for the pasta")
    assert (d.seconds, d.before, d.after) == (600, "set a timer for", "for the pasta")
    assert find_duration("set a timer") is None
