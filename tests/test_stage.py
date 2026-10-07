"""The pipeline stage's gating: it matches only a Waggle phone's parsable, allowed request."""
from __future__ import annotations

from datetime import timedelta

import pytest
from ovos_bus_client.message import Message

from ovos_plugin_manager.templates.pipeline import ConfidenceMatcherPipeline

from ovos_skill_waggle.pipeline import PLUGIN_ID, SKILL_ID, UTTERANCE_REQUEST
from waggle.fake_phone import DEFAULT_CAPABILITIES
from waggle.messages import CAPABILITIES, Capabilities
from waggle.intents import ACTION_SET_ALARM, ACTION_SET_TIMER, ACTION_SHOW_ALARMS

from conftest import NOW, OTHER_PEER, PEER, TestPipeline, capabilities, utterance

TIMER = "set a timer for 10 minutes"


def match(pipeline, text, peer=PEER, lang="en-US"):
    return pipeline.match_high([text], lang, utterance(text, peer, lang))


def rules(*rules):
    return [{"action": a, "mode": m, **extra} for a, m, extra in rules]


def test_is_a_confidence_matcher(pipeline):
    assert isinstance(pipeline, ConfidenceMatcherPipeline)
    assert PLUGIN_ID == "ovos-waggle-pipeline-plugin"


def test_match_shape(pipeline, phone):
    m = match(pipeline, TIMER)
    assert m.match_type == UTTERANCE_REQUEST == "waggle:utterance"
    assert m.match_data == {"request": "timer.set", "params": {"seconds": 600}, "speak": True}
    assert m.skill_id == SKILL_ID
    assert m.utterance == TIMER


def test_only_the_high_tier_matches(pipeline, phone):
    assert match(pipeline, TIMER) is not None
    msg = utterance(TIMER)
    assert pipeline.match_medium([TIMER], "en-US", msg) is None
    assert pipeline.match_low([TIMER], "en-US", msg) is None
    assert pipeline.match([TIMER], "en-US", msg) is not None


# --- 1. the speaker is a Waggle phone ----------------------------------------

def test_no_capabilities_no_match(pipeline):
    assert match(pipeline, TIMER) is None


def test_other_peer_no_match(pipeline, phone):
    assert match(pipeline, TIMER, peer=OTHER_PEER) is None
    assert match(pipeline, TIMER, peer=None) is None


def announce_version(bus, version):
    bus.emit(Message(CAPABILITIES, {**DEFAULT_CAPABILITIES.to_dict(), "version": version},
                     {"source": PEER, "peer": PEER, "destination": "skills"}))


def test_unsupported_version_no_match(bus, pipeline):
    announce_version(bus, 2)
    assert match(pipeline, TIMER) is None


def test_change_to_unsupported_version_stops_matching(bus, pipeline, phone):
    assert match(pipeline, TIMER) is not None
    announce_version(bus, 99)
    assert match(pipeline, TIMER) is None


def test_new_session_same_client_matches(pipeline, phone):
    # Announced from session-1; the utterance comes from the same client's next session.
    assert match(pipeline, TIMER, peer="Wiggins/0.3.0::7::pixel-8::session-2") is not None


def test_match_uses_peer_not_source(pipeline, phone):
    msg = utterance(TIMER)
    msg.context["source"] = "something-else"
    assert pipeline.match_high([TIMER], "en-US", msg) is not None


# --- 2. a request it understands ---------------------------------------------

@pytest.mark.parametrize("text", ["what's the weather", "set a timer", "cancel my alarm",
                                  "tell me a joke", ""])
def test_unparsed_no_match(pipeline, phone, text):
    assert match(pipeline, text) is None


def test_non_english_no_match(pipeline, phone):
    assert match(pipeline, TIMER, lang="de-DE") is None


def test_tries_each_utterance(pipeline, phone):
    m = pipeline.match_high(["what's the weather", TIMER], "en-US", utterance(TIMER))
    assert m.match_data["params"] == {"seconds": 600} and m.utterance == TIMER


@pytest.mark.parametrize("text,request_", [
    ("set an alarm for 6:30 am", "alarm.set"),
    ("wake me up at 7", "alarm.set"),
    ("set a timer for an hour and a half", "timer.set"),
    ("show my alarms", "alarms.show"),
])
def test_each_request_matches(pipeline, phone, text, request_):
    assert match(pipeline, text).match_data["request"] == request_


def test_disabled_request_no_match(make_pipeline, make_phone):
    pipeline = make_pipeline({"enable_timer_set": False})
    make_phone()
    assert match(pipeline, TIMER) is None
    assert match(pipeline, "set an alarm for 6 am") is not None


# --- 3. the phone would allow it ---------------------------------------------

def test_blocked_by_rule_no_match(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=rules(
        (ACTION_SET_TIMER, "block", {}), (ACTION_SET_ALARM, "run", {}))))
    assert match(pipeline, TIMER) is None
    assert match(pipeline, "set an alarm for 6 am") is not None


def test_unmatched_block_no_match(pipeline, make_phone):
    make_phone(capabilities=capabilities(unmatched="block",
                                         rules=rules((ACTION_SET_ALARM, "run", {}))))
    assert match(pipeline, TIMER) is None
    assert match(pipeline, "show my alarms") is None


def test_unmatched_ask_matches(pipeline, make_phone):
    make_phone(capabilities=capabilities(unmatched="ask", rules=[]))
    assert match(pipeline, TIMER) is not None


def test_ask_rule_matches(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=rules((ACTION_SET_TIMER, "ask", {}))))
    assert match(pipeline, TIMER) is not None


def test_more_specific_block_rule_wins(pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=rules(
        (ACTION_SHOW_ALARMS, "run", {}),
        (ACTION_SHOW_ALARMS, "block", {"package": "com.example.clock"}))))
    assert match(pipeline, "show my alarms") is not None


def test_package_override_meets_package_rule(make_pipeline, make_phone):
    pipeline = make_pipeline({"package_alarms_show": "com.example.clock"})
    make_phone(capabilities=capabilities(rules=rules(
        (ACTION_SHOW_ALARMS, "run", {}),
        (ACTION_SHOW_ALARMS, "block", {"package": "com.example.clock"}))))
    assert match(pipeline, "show my alarms") is None


def test_latest_announcement_decides(pipeline, phone):
    assert match(pipeline, TIMER) is not None
    phone.capabilities = capabilities(rules=rules((ACTION_SET_TIMER, "block", {})))
    phone.announce()
    assert match(pipeline, TIMER) is None


# --- times on the phone's clock ----------------------------------------------

@pytest.mark.parametrize("tz,hour,minute", [
    ("America/Chicago", 14, 30),   # NOW is 14:10 in Chicago
    ("Asia/Tokyo", 4, 30),         # ...and 04:10 in Tokyo
    ("Asia/Kolkata", 1, 0),        # ...and 00:40 in Kolkata
])
def test_times_in_the_phones_timezone(pipeline, make_phone, tz, hour, minute):
    make_phone(capabilities=capabilities(timezone=tz))
    m = match(pipeline, "set an alarm in 20 minutes")
    assert m.match_data["params"] == {"hour": hour, "minute": minute}


def test_am_pm_guess_in_the_phones_timezone(pipeline, make_phone):
    make_phone(capabilities=capabilities(timezone="Asia/Tokyo"))   # 04:10 there
    assert match(pipeline, "set an alarm for 6:30").match_data["params"]["hour"] == 6
    make_phone(capabilities=capabilities(timezone="America/Chicago"))  # 14:10 there
    assert match(pipeline, "set an alarm for 6:30").match_data["params"]["hour"] == 18


def test_phone_without_timezone_uses_the_hubs(pipeline, make_phone):
    data = DEFAULT_CAPABILITIES.to_dict()
    del data["timezone"]
    make_phone(capabilities=Capabilities.from_dict(data))
    expected = NOW.astimezone() + timedelta(minutes=20)  # the hub's local zone
    m = match(pipeline, "set an alarm in 20 minutes")
    assert m.match_data["params"] == {"hour": expected.hour, "minute": expected.minute}


def test_parser_errors_never_break_the_pipeline(pipeline, phone, monkeypatch):
    import ovos_skill_waggle.pipeline as module

    def boom(*args):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(module, "parse_request", boom)
    assert match(pipeline, TIMER) is None


def test_config_defaults_to_mycroft_conf(bus, monkeypatch):
    import ovos_skill_waggle.pipeline as module

    class FakeConfiguration(dict):
        def __init__(self):
            super().__init__({"intents": {PLUGIN_ID: {"response_timeout_s": 2}}})

    monkeypatch.setattr(module, "Configuration", FakeConfiguration)
    pipeline = TestPipeline(bus)
    try:
        assert pipeline.waggle_settings.response_timeout_s == 2.0
    finally:
        pipeline.default_shutdown()
