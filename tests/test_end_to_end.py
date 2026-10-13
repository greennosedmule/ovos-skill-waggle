"""End to end on a FakeBus: ovos-core 3.x's IntentService loads the plugin from its entry point.

A padacioso intent registered for a stand-in alerts skill plays the stock
timer skill that runs later in the pipeline. An utterance from the Waggle
phone reaches the fake phone; the same utterance from another satellite, or
one the phone blocks, falls through to the alerts skill.

ovos-core 3 dispatches a padatious intent without its ``.intent`` suffix,
reports "no match" as ``ovos.intent.unmatched``, and speaks ``get_response``
questions as ``ovos.utterance.speak`` only.
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
from waggle.intents import (  # noqa: E402
    ACTION_DISMISS_ALARM, ACTION_DISMISS_TIMER, ACTION_SET_TIMER, ACTION_SHOW_TIMERS,
)
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
ALERTS_SKILL = "alerts.stand-in"
ALERTS_TIMER = f"{ALERTS_SKILL}:set_timer.intent"
ALERTS_CANCEL = f"{ALERTS_SKILL}:cancel_timer.intent"
# What a match on it is dispatched as: without the suffix.
ALERTS_DISPATCH = (ALERTS_TIMER.removesuffix(".intent"),)
ALERTS_CANCEL_DISPATCH = ALERTS_CANCEL.removesuffix(".intent")
UNMATCHED = ("ovos.intent.unmatched",)
HANDLED = "ovos.utterance.handled"


@pytest.fixture
def service(bus):
    service = IntentService(bus, config={})
    plugin = service.pipeline_plugins[PLUGIN_ID]
    plugin.__class__ = TestPipeline  # its finalizer, quiet at interpreter exit
    plugin.clock = lambda: NOW
    plugin.waggle_settings = plugin.waggle_settings.__class__.from_config(
        {"response_timeout_s": 0.3})
    # The intent parsers drop a registration without a skill_id in the context.
    bus.emit(Message("padatious:register_intent", {
        "name": ALERTS_TIMER, "lang": "en-US",
        "samples": ["set a timer for {duration}", "start a timer for {duration}"]},
        {"skill_id": ALERTS_SKILL}))
    bus.emit(Message("padatious:register_intent", {
        "name": ALERTS_CANCEL, "lang": "en-US",
        "samples": ["stop the timer", "cancel my timer", "cancel the timer"]},
        {"skill_id": ALERTS_SKILL}))
    yield service
    service.shutdown()
    plugin.default_shutdown()


def alerts(recorder):
    return [m for m in recorder.messages if m.msg_type in ALERTS_DISPATCH]


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
    assert alerts(recorder) == []
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
    taken = alerts(recorder)
    assert len(taken) == 1 and taken[0].context["destination"] == OTHER_PEER


def test_blocked_on_the_phone_falls_through(bus, recorder, service, make_phone):
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    recorder.clear()
    say(bus, "set a timer for 10 minutes", PEER)
    assert phone.log == []
    assert len(alerts(recorder)) == 1


def test_unparsed_request_falls_through(bus, recorder, service, make_phone):
    make_phone()
    recorder.clear()
    say(bus, "what's the weather like", PEER)
    assert recorder.of(INTENT) == []
    assert any(recorder.of(t) for t in UNMATCHED)


def test_new_session_of_the_same_phone(bus, recorder, service, make_phone):
    phone = make_phone()  # announced from session-1
    # A new HiveMind session (Clear conversation) changes the peer id but not the client id,
    # and the phone doesn't announce again.
    phone.peer = "Wiggins/0.3.0::7::pixel-8::session-2"
    recorder.clear()
    say(bus, "set an alarm for 6:30 am", phone.peer)
    assert [m.data["utterance"] for m in recorder.of("speak")] == ["Alarm set for 6:30 AM."]
    assert recorder.of(INTENT)[0].context["destination"] == phone.peer


def test_follow_up_question_through_converse(bus, recorder, service, make_phone):
    """ "Set a timer" asks how long; the answer comes back through ovos-core's converse stage."""
    import threading

    phone = make_phone()
    recorder.clear()
    first = threading.Thread(target=say, args=(bus, "set a timer", PEER), daemon=True)
    first.start()

    question = _wait_for_question(recorder)
    assert question.data["utterance"] == "For how long?"
    assert question.context["destination"] == PEER

    # The phone answers in the session the question came with, as Wiggins does.
    bus.emit(Message("recognizer_loop:utterance", {"utterances": ["10 minutes"], "lang": "en-US"},
                     {"source": PEER, "peer": PEER, "destination": "skills",
                      "session": question.context["session"]}))
    first.join(10)
    assert not first.is_alive()

    intents = [e.request.data for e in phone.log if "action" in e.request.data]
    assert [i["action"] for i in intents] == [ACTION_SET_TIMER]
    assert intents[0]["extras"]["android.intent.extra.alarm.LENGTH"]["value"] == 600
    assert alerts(recorder) == []
    assert [m.data["utterance"] for m in recorder.speech()][-1] == "Timer set for 10 minutes."


def _wait_for_question(recorder, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        question = next((m for m in recorder.speech() if m.data.get("expect_response")), None)
        if question is not None:
            return question
        time.sleep(0.05)
    raise AssertionError(recorder.types())


def test_message_body_keeps_the_wording_as_said(bus, recorder, service, make_phone):
    """ovos-core's normalizer rewrites the answer and strips its "!"; the text keeps it as said."""
    import threading

    phone = make_phone(contacts=[{"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"}]}],
                       ask_answer="accept")
    recorder.clear()
    first = threading.Thread(target=say, args=(bus, "text mom", PEER), daemon=True)
    first.start()
    question = _wait_for_question(recorder)
    assert question.data["utterance"] == "What should the message say?"
    bus.emit(Message("recognizer_loop:utterance",
                     {"utterances": ["I'm running late, start without me!"], "lang": "en-US"},
                     {"source": PEER, "peer": PEER, "destination": "skills",
                      "session": question.context["session"]}))
    first.join(10)
    intents = [e.request.data for e in phone.log if "action" in e.request.data]
    assert intents[0]["extras"]["sms_body"]["value"] == "I'm running late, start without me!"


def test_inline_message_body_keeps_the_wording_as_said(bus, recorder, service, make_phone):
    phone = make_phone(contacts=[{"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"}]}])
    recorder.clear()
    session = Session(f"session-of-{PEER}", pipeline=PIPELINE)
    bus.emit(Message("recognizer_loop:utterance",
                     {"utterances": ["text Mom saying I'm late, Sorry!"], "lang": "en-US"},
                     {"source": PEER, "peer": PEER, "destination": "skills",
                      "session": session.serialize()}))
    intents = [e.request.data for e in phone.log if "action" in e.request.data]
    assert intents[0]["extras"]["sms_body"]["value"] == "I'm late, Sorry!"


# --- stopping timers and dismissing alarms -------------------------------------

def cancels_on_the_hub(recorder):
    return [m for m in recorder.messages if m.msg_type == ALERTS_CANCEL_DISPATCH]


def test_stop_the_timer_from_the_phone(bus, recorder, service, make_phone):
    """ "Stop the timer" passes OVOS's stop stage (exact words only) and reaches the phone."""
    phone = make_phone()
    recorder.clear()
    say(bus, "stop the timer", PEER)
    assert [e.request.data["action"] for e in phone.log] == [ACTION_DISMISS_TIMER,
                                                             ACTION_SHOW_TIMERS]
    assert cancels_on_the_hub(recorder) == []
    speak = recorder.of("speak")
    assert [m.data["utterance"] for m in speak] == [
        "I've stopped any ringing timer. Here are your timers."]
    assert speak[0].context["destination"] == PEER
    handled = recorder.of(HANDLED)
    assert len(handled) == 1 and handled[0].context["destination"] == PEER


def test_stop_the_timer_from_another_satellite_falls_through(bus, recorder, service, make_phone):
    phone = make_phone()
    recorder.clear()
    say(bus, "stop the timer", OTHER_PEER)
    assert phone.log == [] and recorder.of(INTENT) == []
    taken = cancels_on_the_hub(recorder)
    assert len(taken) == 1 and taken[0].context["destination"] == OTHER_PEER


def test_dismiss_alarm_asks_on_the_phone(bus, recorder, service, make_phone):
    phone = make_phone()  # accepts the ask card
    recorder.clear()
    say(bus, "turn off my 6:30 am alarm", PEER)
    sent = [e.request.data for e in phone.log]
    assert [i["action"] for i in sent] == [ACTION_DISMISS_ALARM]
    assert sent[0]["extras"]["android.intent.extra.alarm.SEARCH_MODE"]["value"] == "android.time"
    assert [m.data["utterance"] for m in recorder.of("speak")] == [
        "Confirm on your phone.", "I've asked your Clock to dismiss the 6:30 AM alarm."]


def test_phone_without_the_new_rules_falls_through(bus, recorder, service, make_phone):
    """A phone that hasn't got the new default rules blocks them, so OVOS keeps them."""
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_SET_TIMER, "mode": "run"}]))
    recorder.clear()
    say(bus, "stop the timer", PEER)
    assert phone.log == []
    assert len(cancels_on_the_hub(recorder)) == 1


def test_named_alarm_asks_its_time_through_converse(bus, recorder, service, make_phone):
    """ "Cancel my work alarm" asks its time (labels don't match reliably), then dismisses
    by time, after the phone's own confirmation."""
    import threading

    phone = make_phone()
    recorder.clear()
    first = threading.Thread(target=say, args=(bus, "cancel my work alarm", PEER), daemon=True)
    first.start()
    question = _wait_for_question(recorder)
    assert question.data["utterance"] == "What time is your work alarm set for?"
    bus.emit(Message("recognizer_loop:utterance", {"utterances": ["6:30 am"], "lang": "en-US"},
                     {"source": PEER, "peer": PEER, "destination": "skills",
                      "session": question.context["session"]}))
    first.join(10)
    assert not first.is_alive()
    sent = [e.request.data for e in phone.log]
    assert [i["action"] for i in sent] == [ACTION_DISMISS_ALARM]
    extras = {k: v["value"] for k, v in sent[0]["extras"].items()}
    assert extras == {"android.intent.extra.alarm.SEARCH_MODE": "android.time",
                      "android.intent.extra.alarm.HOUR": 6,
                      "android.intent.extra.alarm.MINUTES": 30}
