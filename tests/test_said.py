"""Utterances as said: recorded per session before ovos-core rewrites them, for text messages."""
from __future__ import annotations

import threading
import time

import pytest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

from ovos_skill_waggle.said import RAW_UTTERANCES, SaidUtterances, in_context, reads_as
from waggle.intents import EXTRA_SMS_BODY

from conftest import PEER

MOM = {"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"}]}


def said(text, session_id="s1", peer=PEER):
    context = {"source": peer, "peer": peer, "destination": "skills",
               "session": Session(session_id).serialize()}
    return Message("recognizer_loop:utterance", {"utterances": [text], "lang": "en-US"}, context)


@pytest.mark.parametrize("heard,as_said,same", [
    ("i am running late , start without me", "I'm running late, start without me!", True),
    ("text mom saying i am late sorry", "Text Mom saying I'm late, sorry!", True),
    ("what is the weather", "Text Mom saying I'm late, sorry!", False),
    ("anything", "", False),
])
def test_reads_as(heard, as_said, same):
    assert reads_as(heard, as_said) is same


def test_finds_the_utterance_as_said():
    store = SaidUtterances()
    store.record(said("I'm late, sorry!"))
    assert store.as_said("s1", "i am late , sorry") == "I'm late, sorry!"
    assert store.as_said("s2", "i am late , sorry") is None  # another session
    assert store.as_said("s1", "set a timer") is None


def test_newest_first():
    store = SaidUtterances()
    store.record(said("On my way."))
    store.record(said("On my way!"))
    assert store.as_said("s1", "on my way") == "On my way!"


def test_old_utterances_expire():
    now = [100.0]
    store = SaidUtterances(keep_s=60, clock=lambda: now[0])
    store.record(said("On my way!"))
    now[0] += 61
    assert store.as_said("s1", "on my way") is None


def test_ignores_messages_without_utterances():
    store = SaidUtterances()
    store.record(Message("recognizer_loop:utterance", {}, {}))
    store.record(Message("recognizer_loop:utterance", {"utterances": [None, " "]}, {}))
    assert store.as_said("default", "") is None


def test_waits_for_the_utterance():
    store = SaidUtterances()
    threading.Timer(0.1, store.record, args=(said("Running late!"),)).start()
    start = time.monotonic()
    assert store.as_said("s1", "running late", wait_s=2) == "Running late!"
    assert time.monotonic() - start < 1.5


def test_stage_takes_the_body_as_said(bus, pipeline, make_phone):
    make_phone(contacts=[MOM])
    bus.emit(said("Text Mom saying I'm late, sorry!"))
    # What ovos-core's normalizer passes on: its rewrite, then the original without the "!".
    utt = said("Text Mom saying I'm late, sorry")
    match = pipeline.match_high(["text mom saying i am late , sorry",
                                 "Text Mom saying I'm late, sorry"], "en-US", utt)
    assert match.match_data["params"] == {"name": "mom", "body": "I'm late, sorry!"}


def test_stage_falls_back_to_the_original_wording(bus, pipeline, make_phone):
    make_phone(contacts=[MOM])
    utt = said("x")
    match = pipeline.match_high(["text mom saying i am late , sorry",
                                 "Text Mom saying I'm late, sorry"], "en-US", utt)
    assert match.match_data["params"]["body"] == "I'm late, sorry"


def test_handler_sends_the_body_as_said(bus, recorder, pipeline, make_phone):
    phone = make_phone(contacts=[MOM])
    bus.emit(said("Text Mom saying I'm late, sorry!"))
    utt = said("Text Mom saying I'm late, sorry")
    match = pipeline.match_high(utt.data["utterances"], "en-US", utt)
    reply = utt.reply(match.match_type, {**utt.data, **match.match_data,
                                         "utterance": match.utterance, "lang": "en-US"})
    reply.context["skill_id"] = match.skill_id
    bus.emit(reply)
    intents = [e.request.data for e in phone.log if "action" in e.request.data]
    assert intents[0]["extras"][EXTRA_SMS_BODY]["value"] == "I'm late, sorry!"


# --- the hub's raw_utterances ----------------------------------------------------

def with_raw(message, raw):
    message.context[RAW_UTTERANCES] = raw
    return message


@pytest.mark.parametrize("raw,found", [
    (["Text Mom saying I'm late, sorry!"], "Text Mom saying I'm late, sorry!"),
    (["set a timer", "Text Mom saying I'm late, sorry!"], "Text Mom saying I'm late, sorry!"),
    (["what is the weather"], None),
    ("Text Mom saying I'm late, sorry!", None),  # not a list
    (["Text Mom saying I'm late, sorry!", 3], None),  # not all strings
    ([], None),
    (None, None),
])
def test_in_context(raw, found):
    assert in_context(with_raw(said("x"), raw), "text mom saying i am late , sorry") == found


def test_in_context_without_the_key():
    assert in_context(said("x"), "text mom saying i am late , sorry") is None


def test_stage_takes_the_body_from_raw_utterances_without_waiting(bus, pipeline, make_phone):
    """Nothing recorded and a long wait allowed: the context's copy is used at once."""
    make_phone(contacts=[MOM])
    pipeline.wait_for_said_s = 5.0
    utt = with_raw(said("Text Mom saying I'm late, sorry"), ["Text Mom saying I'm late, sorry!"])
    start = time.monotonic()
    match = pipeline.match_high(["text mom saying i am late , sorry",
                                 "Text Mom saying I'm late, sorry"], "en-US", utt)
    assert time.monotonic() - start < 1.0
    assert match.match_data["params"] == {"name": "mom", "body": "I'm late, sorry!"}


def test_raw_utterances_win_over_the_recorded_utterance(bus, pipeline, make_phone):
    make_phone(contacts=[MOM])
    bus.emit(said("Text Mom saying I'm late, sorry."))
    utt = with_raw(said("x"), ["Text Mom saying I'm late, sorry!"])
    match = pipeline.match_high(["text mom saying i am late , sorry"], "en-US", utt)
    assert match.match_data["params"]["body"] == "I'm late, sorry!"


def test_stage_falls_back_to_the_recording_when_raw_utterances_dont_match(bus, pipeline,
                                                                         make_phone):
    make_phone(contacts=[MOM])
    bus.emit(said("Text Mom saying I'm late, sorry!"))
    utt = with_raw(said("x"), ["something else entirely"])
    match = pipeline.match_high(["text mom saying i am late , sorry"], "en-US", utt)
    assert match.match_data["params"]["body"] == "I'm late, sorry!"
