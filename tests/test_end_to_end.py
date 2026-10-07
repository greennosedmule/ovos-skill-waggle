"""End to end on a FakeBus: ovos-core 2.x's IntentService loads the plugin from its entry point.

A padacioso intent registered for a stand-in alerts skill plays the stock
timer skill that runs later in the pipeline. An utterance from the Waggle
phone reaches the fake phone; the same utterance from another satellite, or
one the phone blocks, falls through to the alerts skill.
"""
from __future__ import annotations

import pytest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

pytest.importorskip("ovos_core")
pytest.importorskip("padacioso")

from ovos_core.intent_services.service import IntentService  # noqa: E402
from ovos_plugin_manager.pipeline import OVOSPipelineFactory  # noqa: E402

from ovos_skill_waggle.pipeline import PLUGIN_ID, SKILL_ID  # noqa: E402
from ovos_skill_waggle.requests import REQUEST_RESPONSE  # noqa: E402
from waggle.intents import ACTION_SET_TIMER  # noqa: E402
from waggle.messages import INTENT  # noqa: E402

from conftest import NOW, OTHER_PEER, PEER, TestPipeline, capabilities  # noqa: E402

# SPEC's pipeline: Waggle right after converse, ahead of the intent parsers.
PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    f"{PLUGIN_ID}-high",
    "ovos-padacioso-pipeline-plugin-high",
    "ovos-padacioso-pipeline-plugin-medium",
]
ALERTS_TIMER = "alerts.stand-in:set_timer.intent"
HANDLED = "ovos.utterance.handled"


@pytest.fixture
def service(bus):
    service = IntentService(bus, config={})
    plugin = service.pipeline_plugins[PLUGIN_ID]
    plugin.__class__ = TestPipeline  # its finalizer, quiet at interpreter exit
    plugin.clock = lambda: NOW
    plugin.waggle_settings = plugin.waggle_settings.__class__.from_config(
        {"response_timeout_s": 0.3})
    bus.emit(Message("padatious:register_intent", {
        "name": ALERTS_TIMER, "lang": "en-US",
        "samples": ["set a timer for {duration}", "start a timer for {duration}"]}))
    yield service
    service.shutdown()
    plugin.default_shutdown()


def say(bus, text, peer):
    session = Session(f"session-of-{peer}", pipeline=PIPELINE)
    context = {"source": peer, "peer": peer, "destination": "skills",
               "session": session.serialize()}
    bus.emit(Message("recognizer_loop:utterance", {"utterances": [text], "lang": "en-US"},
                     context))


def test_plugin_is_installed():
    assert PLUGIN_ID in OVOSPipelineFactory.get_installed_pipeline_ids()
    assert f"{PLUGIN_ID}-high" in OVOSPipelineFactory.get_installed_pipeline_matcher_ids()


def test_waggle_phone_reaches_the_phone(bus, recorder, service, make_phone):
    phone = make_phone()
    recorder.clear()
    say(bus, "set a timer for 10 minutes", PEER)

    assert [e.request.data["action"] for e in phone.log] == [ACTION_SET_TIMER]
    assert phone.responses[0].ok
    assert recorder.of(ALERTS_TIMER) == []
    assert recorder.of(f"{SKILL_ID}.activate")  # ovos-core activated the skill
    speak = recorder.of("speak")
    assert [m.data["utterance"] for m in speak] == ["Timer set for 10 minutes."]
    assert speak[0].context["destination"] == PEER
    assert recorder.of(REQUEST_RESPONSE)[0].data["ok"] is True
    handled = recorder.of(HANDLED)
    assert len(handled) == 1 and handled[0].context["destination"] == PEER


def test_other_satellite_falls_through(bus, recorder, service, make_phone):
    phone = make_phone()
    recorder.clear()
    say(bus, "set a timer for 10 minutes", OTHER_PEER)
    assert phone.log == [] and recorder.of(INTENT) == []
    alerts = recorder.of(ALERTS_TIMER)
    assert len(alerts) == 1 and alerts[0].context["destination"] == OTHER_PEER


def test_blocked_on_the_phone_falls_through(bus, recorder, service, make_phone):
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    recorder.clear()
    say(bus, "set a timer for 10 minutes", PEER)
    assert phone.log == []
    assert len(recorder.of(ALERTS_TIMER)) == 1


def test_unparsed_request_falls_through(bus, recorder, service, make_phone):
    make_phone()
    recorder.clear()
    say(bus, "what's the weather like", PEER)
    assert recorder.of(INTENT) == []
    assert recorder.of("complete_intent_failure")


def test_new_session_of_the_same_phone(bus, recorder, service, make_phone):
    phone = make_phone()  # announced from session-1
    # A new HiveMind session (Clear conversation) changes the peer id but not the client id,
    # and the phone doesn't announce again.
    phone.peer = "Wiggins/0.3.0::7::pixel-8::session-2"
    recorder.clear()
    say(bus, "set an alarm for 6:30 am", phone.peer)
    assert [m.data["utterance"] for m in recorder.of("speak")] == ["Alarm set for 6:30 AM."]
    assert recorder.of(INTENT)[0].context["destination"] == phone.peer
