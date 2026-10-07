"""The stage for S3: gating queries and lookups, the app catalog, and follow-up questions."""
from __future__ import annotations

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.apps import AppCatalog, names_app
from ovos_skill_waggle.pipeline import UTTERANCE_REQUEST
from waggle.intents import ACTION_DIAL, ACTION_SET_TIMER
from waggle.messages import APPS_LIST, CALENDAR_NEXT, CONTACTS_LOOKUP, INTENT, QUERY

from conftest import PEER, capabilities, match_message, utterance

APPS = [{"label": "Spotify: Music and Podcasts", "package": "com.spotify.music"},
        {"label": "Camera", "package": "app.grapheneos.camera"}]


def match(pipeline, text, peer=PEER):
    return pipeline.match_high([text], "en-US", utterance(text, peer))


# --- gating ------------------------------------------------------------------

@pytest.mark.parametrize("text,request_name", [
    ("what's my next appointment", "calendar.next"),
    ("call mom", "contact.call"),
    ("text mom saying hi", "message.compose"),
    ("open the camera app", "app.open"),
])
def test_matches_when_shared(pipeline, make_phone, text, request_name):
    make_phone(apps=APPS)
    m = match(pipeline, text)
    assert m is not None and m.match_data["request"] == request_name


@pytest.mark.parametrize("text,query", [
    ("what's my next appointment", CALENDAR_NEXT),
    ("call mom", CONTACTS_LOOKUP),
    ("text mom saying hi", CONTACTS_LOOKUP),
    ("open the camera app", APPS_LIST),
])
def test_falls_through_when_not_shared(pipeline, make_phone, text, query):
    shared = [q for q in (CALENDAR_NEXT, CONTACTS_LOOKUP, APPS_LIST) if q != query]
    make_phone(capabilities=capabilities(queries=shared))
    assert match(pipeline, text) is None


def test_a_number_said_aloud_needs_no_contacts(pipeline, make_phone):
    make_phone(capabilities=capabilities(queries=[]))
    assert match(pipeline, "call 555 1234").match_data["params"] == {"name": "555 1234"}


def test_falls_through_when_dialing_is_blocked(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_DIAL, "mode": "block"}]))
    assert match(pipeline, "call mom") is None


def test_disabled_on_the_hub(make_pipeline, make_phone):
    pipeline = make_pipeline({"enable_calendar_next": False})
    make_phone()
    assert match(pipeline, "what's my next appointment") is None


# --- apps by name ------------------------------------------------------------

def test_open_an_app_the_phone_has(pipeline, make_phone):
    make_phone(apps=APPS)
    assert match(pipeline, "open spotify").match_data == {
        "request": "app.open", "params": {"name": "spotify"}, "speak": True}


def test_open_something_that_isnt_an_app(pipeline, make_phone):
    make_phone(apps=APPS)
    assert match(pipeline, "open the garage door") is None


def test_open_without_an_app_list(pipeline, make_phone):
    make_phone(apps=APPS, capabilities=capabilities(queries=[CALENDAR_NEXT]))
    assert match(pipeline, "open spotify") is None


def test_catalog_fetches_on_announce(pipeline, make_phone, recorder):
    recorder.clear()
    make_phone(apps=APPS)
    query = recorder.of(QUERY)
    assert len(query) == 1 and query[0].data["params"] == {"limit": 500}
    assert query[0].context["destination"] == PEER
    assert pipeline.apps.labels("7") == ["Spotify: Music and Podcasts", "Camera"]


def test_catalog_refreshes_only_when_stale(pipeline, make_phone, recorder):
    phone = make_phone(apps=APPS)
    recorder.clear()
    phone.announce()
    assert recorder.of(QUERY) == []


def test_catalog_forgets_a_phone_that_stops_sharing(pipeline, make_phone):
    phone = make_phone(apps=APPS)
    phone.capabilities = capabilities(queries=[])
    phone.announce()
    assert pipeline.apps.labels("7") is None


def test_catalog_staleness(bus, make_phone, pipeline):
    phone = make_phone(apps=APPS)
    t = [0.0]
    catalog = AppCatalog(bus, lambda: 0.3, refresh_s=60, clock=lambda: t[0])
    known = pipeline.capabilities.get(PEER)
    origin = Message("waggle.capabilities", {}, {"source": PEER, "destination": "skills"})
    catalog.refresh(known, origin, wait=True)
    catalog.refresh(known, origin, wait=True)
    t[0] = 61
    catalog.refresh(known, origin, wait=True)
    assert [e.request.data["name"] for e in phone.log] == [APPS_LIST, APPS_LIST]


@pytest.mark.parametrize("name,known", [
    ("spotify", True), ("Camera", True), ("the camera", True), ("garage door", False),
    ("music", True), ("cam", False), ("", False),
])
def test_names_app(name, known):
    assert names_app(name, ["Spotify: Music and Podcasts", "Camera"]) is known


# --- requests that need a follow-up question ---------------------------------

def test_incomplete_request_matches_with_missing(pipeline, make_phone):
    make_phone()
    assert match(pipeline, "set a timer").match_data == {
        "request": "timer.set", "params": {}, "speak": True, "missing": "duration"}


def test_incomplete_request_blocked_on_the_phone(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    assert match(pipeline, "set a timer") is None


class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, origin, dialog, data):
        self.asked.append(dialog)
        return self.answers.pop(0) if self.answers else None


def run_utterance(bus, recorder, pipeline, text):
    utt = utterance(text)
    m = pipeline.match_high([text], "en-US", utt)
    assert m is not None and m.match_type == UTTERANCE_REQUEST
    recorder.clear()
    bus.emit(match_message(utt, m))


def spoken(recorder):
    return [m.data["utterance"] for m in recorder.of("speak")]


@pytest.mark.parametrize("text,answer,said", [
    ("set a timer", "10 minutes", "Timer set for 10 minutes."),
    ("start a pasta timer", "8 minutes", "pasta timer set for 8 minutes."),
    ("set an alarm", "6:30 am", "Alarm set for 6:30 AM."),
    ("wake me up", "7", "Alarm set for 7:00 AM."),
])
def test_follow_up_answer_runs_the_request(bus, recorder, pipeline, make_phone, text, answer,
                                           said):
    phone = make_phone()
    pipeline._ask = Answers(answer)
    run_utterance(bus, recorder, pipeline, text)
    assert spoken(recorder) == [said]
    assert len(recorder.of(INTENT)) == 1 and phone.responses[0].ok


def test_follow_up_message_body_keeps_its_case(bus, recorder, pipeline, make_phone):
    make_phone(contacts=[{"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"}]}])
    pipeline._ask = Answers("I'm running late, sorry!")
    run_utterance(bus, recorder, pipeline, "text mom")
    assert pipeline._ask.asked == ["ask_body"]
    intent = recorder.of(INTENT)[0].data
    assert intent["extras"]["sms_body"]["value"] == "I'm running late, sorry!"


def test_follow_up_cancelled(bus, recorder, pipeline, make_phone):
    make_phone()
    pipeline._ask = Answers(None)
    run_utterance(bus, recorder, pipeline, "set a timer")
    assert spoken(recorder) == ["Okay, cancelled."]
    assert recorder.of(INTENT) == []


def test_follow_up_asks_again_once(bus, recorder, pipeline, make_phone):
    make_phone()
    pipeline._ask = Answers("purple", "five minutes")
    run_utterance(bus, recorder, pipeline, "set a timer")
    assert pipeline._ask.asked == ["ask_duration", "ask_duration"]
    assert spoken(recorder) == ["Sorry, I didn't catch that.", "Timer set for 5 minutes."]


def test_follow_up_gives_up(bus, recorder, pipeline, make_phone):
    make_phone()
    pipeline._ask = Answers("purple", "orange")
    run_utterance(bus, recorder, pipeline, "set a timer")
    assert spoken(recorder) == ["Sorry, I didn't catch that.", "Okay, cancelled."]
    assert recorder.of(INTENT) == []


def test_real_ask_speaks_to_the_phone_expecting_an_answer(bus, recorder, pipeline, make_phone,
                                                         monkeypatch):
    make_phone()
    asked = []

    def fake_get_response(dialog, data, num_retries, message, wait):
        asked.append((dialog, message, wait))
        return "ten minutes"

    monkeypatch.setattr(pipeline, "get_response", fake_get_response)
    run_utterance(bus, recorder, pipeline, "set a timer")
    dialog, message, wait = asked[0]
    assert dialog == "ask_duration" and wait is False
    # get_response forwards the message it's given, so it must be addressed to the phone.
    assert message.forward("speak", {}).context["destination"] == PEER
    assert spoken(recorder) == ["Timer set for 10 minutes."]
