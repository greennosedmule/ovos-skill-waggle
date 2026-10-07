"""Utterance to request for S3: calendar, calls, texts, apps, incomplete requests and answers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ovos_skill_waggle.parsing import (
    MISSING_BODY, MISSING_DURATION, MISSING_TIME, PHONE_TYPE_SYNONYMS, answer_alarm_time,
    answer_duration, choose, parse_request, parse_utterance,
)

CHICAGO = ZoneInfo("America/Chicago")
# 14:10 on the phone's clock, Wednesday 7 October 2026.
AFTERNOON = datetime(2026, 10, 7, 14, 10, tzinfo=CHICAGO)


def parsed(text, now=AFTERNOON):
    p = parse_utterance(text, "en-US", now)
    return None if p is None else (p.request, p.params, p.missing, p.check_app)


# --- calendar ----------------------------------------------------------------

@pytest.mark.parametrize("text,params", [
    ("What's my next appointment", {"count": 1}),
    ("what's my next appointment?", {"count": 1}),
    ("when is my next meeting", {"count": 1}),
    ("where's my next meeting", {"count": 1}),
    ("what's my next event", {"count": 1}),
    ("what are my next 3 meetings", {"count": 3}),
    ("what are my next three events", {"count": 3}),
    ("what's on my calendar", {"count": 3}),
    ("what's on my schedule", {"count": 3}),
    ("do I have any appointments coming up", {"count": 3}),
    ("what's on my calendar today", {"day": "2026-10-07"}),
    ("what do I have today", {"day": "2026-10-07"}),
    ("what's on my agenda this afternoon", {"day": "2026-10-07"}),
    ("what do I have tomorrow", {"day": "2026-10-08"}),
    ("do I have any meetings tomorrow", {"day": "2026-10-08"}),
    ("am I free on Friday", {"day": "2026-10-09"}),
    ("what's on my calendar Wednesday", {"day": "2026-10-07"}),
    ("what's on my calendar next Wednesday", {"day": "2026-10-14"}),
    ("what's on my calendar monday", {"day": "2026-10-12"}),
])
def test_calendar(text, params):
    assert parsed(text) == ("calendar.next", params, None, False)


@pytest.mark.parametrize("text", [
    "schedule a meeting with Bob tomorrow",
    "add an appointment for Friday",
    "cancel my next meeting",
    "move my dentist appointment",
    "what do I have",  # no calendar word, no day
    "what's the weather tomorrow",
    "tell me about the event horizon",
])
def test_not_a_calendar_question(text):
    assert parsed(text) is None or parsed(text)[0] != "calendar.next"


# --- calls -------------------------------------------------------------------

@pytest.mark.parametrize("text,params", [
    ("call mom", {"name": "mom"}),
    ("Call Mom.", {"name": "mom"}),
    ("please call my mom", {"name": "mom"}),
    ("call Sam Lee", {"name": "sam lee"}),
    ("phone dad", {"name": "dad"}),
    ("dial the plumber", {"name": "plumber"}),
    ("give Jenny a call", {"name": "jenny"}),
    ("call mom on her cell", {"name": "mom", "type": "cell"}),
    ("call mom on her mobile", {"name": "mom", "type": "cell"}),
    ("call dad at work", {"name": "dad", "type": "work"}),
    ("call dad's work number", {"name": "dad", "type": "work"}),
    ("call grandma at home", {"name": "grandma", "type": "home"}),
    ("call 555 1234", {"name": "555 1234"}),
])
def test_call(text, params):
    assert parsed(text) == ("contact.call", params, None, False)


@pytest.mark.parametrize("text", ["call me a taxi", "call it a day", "call back", "call me Ishmael",
                                  "what do you call a fish with no eyes", "call an uber"])
def test_not_a_call(text):
    assert parsed(text) is None


# --- texts -------------------------------------------------------------------

@pytest.mark.parametrize("text,params,missing", [
    ("text Mom saying I'm running late", {"name": "mom", "body": "I'm running late"}, None),
    ("Text Mom that I'm on my way!", {"name": "mom", "body": "I'm on my way!"}, None),
    ("text mom I'm running late.", {"name": "mom", "body": "I'm running late."}, None),
    ("message Jenny: dinner's ready", {"name": "jenny", "body": "dinner's ready"}, None),
    ("Can you text Sam, see you at 5?", {"name": "sam", "body": "see you at 5?"}, None),
    ("send a text to Dad saying call me", {"name": "dad", "body": "call me"}, None),
    ("send a message to John Smith", {"name": "john smith"}, MISSING_BODY),
    ("text mom", {"name": "mom"}, MISSING_BODY),
    ("text my sister", {"name": "sister"}, MISSING_BODY),
])
def test_message(text, params, missing):
    assert parsed(text) == ("message.compose", params, missing, False)


@pytest.mark.parametrize("text", ["read my messages", "any new messages", "what did mom text me"])
def test_not_a_message(text):
    assert parsed(text) is None


# --- apps --------------------------------------------------------------------

@pytest.mark.parametrize("text,name,check", [
    ("open the camera app", "camera", False),
    ("launch the Spotify application", "spotify", False),
    ("open spotify", "spotify", True),
    ("launch maps", "maps", True),
    ("start YouTube", "youtube", True),
    ("open the garage door", "garage door", True),  # the stage checks the phone's apps
])
def test_app(text, name, check):
    assert parsed(text) == ("app.open", {"name": name}, None, check)


def test_app_open_needs_checking_before_it_is_a_request():
    assert parse_request("open spotify", "en-US", AFTERNOON) is None
    assert parse_request("open the spotify app", "en-US", AFTERNOON).params == {"name": "spotify"}


def test_open_my_alarms_is_still_show_alarms():
    assert parsed("open my alarms") == ("alarms.show", {}, None, False)


# --- incomplete timers and alarms --------------------------------------------

@pytest.mark.parametrize("text,request_name,params,missing", [
    ("set a timer", "timer.set", {}, MISSING_DURATION),
    ("start a timer", "timer.set", {}, MISSING_DURATION),
    ("start a pasta timer", "timer.set", {"label": "pasta"}, MISSING_DURATION),
    ("set a timer called tea", "timer.set", {"label": "tea"}, MISSING_DURATION),
    ("set an alarm", "alarm.set", {}, MISSING_TIME),
    ("wake me up", "alarm.set", {}, MISSING_TIME),
])
def test_incomplete(text, request_name, params, missing):
    assert parsed(text) == (request_name, params, missing, False)
    assert parse_request(text, "en-US", AFTERNOON) is None


@pytest.mark.parametrize("text", ["cancel my timer", "stop the alarm", "set an alarm for Friday",
                                  "set a timer for 0 seconds"])
def test_not_incomplete(text):
    assert parsed(text) is None


# --- answers to follow-up questions ------------------------------------------

@pytest.mark.parametrize("answer,seconds", [
    ("10 minutes", 600), ("ten minutes", 600), ("an hour and a half", 5400),
    ("30 seconds", 30), ("five", None), ("never mind", None), ("", None),
])
def test_answer_duration(answer, seconds):
    assert answer_duration(answer) == seconds


@pytest.mark.parametrize("asked,answer,when", [
    ("wake me up", "7", (7, 0)),
    ("set an alarm", "7", (19, 0)),            # the next 7 on the clock after 14:10
    ("set an alarm", "6:30 pm", (18, 30)),
    ("set an alarm", "half past seven", (19, 30)),
    ("set an alarm", "seven in the morning", (7, 0)),
    ("set an alarm", "noon", (12, 0)),
    ("set an alarm", "in 20 minutes", (14, 30)),
    ("set an alarm", "whenever", None),
])
def test_answer_alarm_time(asked, answer, when):
    assert answer_alarm_time(asked, answer, AFTERNOON) == when


@pytest.mark.parametrize("answer,index", [
    ("Sam Lee", 0), ("sam ortiz", 1), ("Ortiz", 1), ("the second one", 1), ("the first", 0),
    ("last", 1), ("2", 1), ("number one", 0), ("Sam", None), ("nobody", None), ("third", None),
])
def test_choose(answer, index):
    assert choose(answer, ["Sam Lee", "Sam Ortiz"]) == index


@pytest.mark.parametrize("answer,index", [
    ("home", 1), ("the work one", 2), ("mobile", 0), ("his cell phone", 0), ("office", 2),
])
def test_choose_phone_type(answer, index):
    assert choose(answer, ["cell", "home", "work"], PHONE_TYPE_SYNONYMS) == index
