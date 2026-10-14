"""Text bodies as said, on ovos-bus-client's real MessageBusClient instead of a FakeBus.

The hub's ovos-core and Waggle share one ``MessageBusClient``. It dispatches a
frame to every listener of its topic as the same ``Message``, on a thread pool,
and bridges ``recognizer_loop:utterance`` to ovos-core's
``ovos.utterance.handle`` as a forwarded copy whose ``data`` is a shallow copy.
So Waggle's record and ovos-core's transformers race, and share the
``utterances`` list. These tests run that path: a client whose "wire" hands
each frame straight back to its own ``on_message``, as the messagebus server
echoes it, with the real ``wait_for_said_s``.
"""
from __future__ import annotations

import threading
import time

import pytest
from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

pytest.importorskip("ovos_core")

from ovos_core.intent_services.service import IntentService  # noqa: E402

from ovos_skill_waggle.handlers import WATCHING  # noqa: E402
from ovos_skill_waggle.pipeline import PLUGIN_ID, WagglePipeline  # noqa: E402
from ovos_skill_waggle.said import RAW_UTTERANCES  # noqa: E402
from waggle.intents import EXTRA_SMS_BODY  # noqa: E402

from conftest import NOW, PEER, TestPipeline  # noqa: E402
from test_end_to_end import PIPELINE  # noqa: E402

MOM = {"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"}]}
# The plugins behind the matchers in PIPELINE.
NEEDED = {"ovos-stop-pipeline-plugin", "ovos-converse-pipeline-plugin", PLUGIN_ID,
          "ovos-padacioso-pipeline-plugin"}


class LoopbackBus(MessageBusClient):
    """A real client whose wire loops back: what it sends, it receives, as the server echoes it."""

    def __init__(self):
        super().__init__()
        self.connected_event.set()

    def _send(self, message: Message):
        self.on_message(message.serialize())


@pytest.fixture
def bus():
    bus = LoopbackBus()
    yield bus
    bus.emitter.remove_all_listeners()


@pytest.fixture
def service(bus):
    # The persona isn't needed here, and loading it is slow.
    service = IntentService(bus, config={"blacklisted_pipelines": ["ovos-persona-pipeline-plugin"]})
    # It loads its pipeline plugins on its own bus message, so on another thread here.
    wait_for(lambda: NEEDED <= set(service.pipeline_plugins), timeout=120)
    plugin = service.pipeline_plugins[PLUGIN_ID]
    plugin.__class__ = TestPipeline
    plugin.wait_for_said_s = WagglePipeline.wait_for_said_s  # the hub's, not the FakeBus's 0
    plugin.clock = lambda: NOW
    plugin.handlers.clock_gap_s = 0.0
    plugin.waggle_settings = plugin.waggle_settings.__class__.from_config(
        {"response_timeout_s": 2})
    yield service
    service.shutdown()
    plugin.default_shutdown()


def wait_for(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("timed out")


def sms_bodies(phone):
    return [e.request.data["extras"][EXTRA_SMS_BODY]["value"] for e in phone.log
            if EXTRA_SMS_BODY in (e.request.data.get("extras") or {})]


def utterance(text, session, **context):
    return Message("recognizer_loop:utterance", {"utterances": [text], "lang": "en-US"},
                   {"source": PEER, "peer": PEER, "destination": "skills",
                    "session": session, **context})


@pytest.mark.parametrize("text,body", [
    ("text Mom saying I'm running late, start without me!", "I'm running late, start without me!"),
    ("Text Mom saying are you coming?", "Are you coming?"),
    ("Text Mom are you coming?", "Are you coming?"),
])
def test_inline_body_keeps_its_punctuation(bus, service, make_phone, text, body):
    phone = make_phone(contacts=[MOM])
    bus.emit(utterance(text, Session(f"session-of-{PEER}", pipeline=PIPELINE).serialize()))
    assert wait_for(lambda: sms_bodies(phone)) == [body]


def test_answer_keeps_its_punctuation(bus, recorder, service, make_phone):
    phone = make_phone(contacts=[MOM], ask_answer="accept")
    recorder.clear()
    bus.emit(utterance("text mom", Session(f"session-of-{PEER}", pipeline=PIPELINE).serialize()))
    question = wait_for(lambda: next((m for m in recorder.speech()
                                      if m.data.get("expect_response")), None))
    assert question.data["utterance"] == "What should the message say?"
    bus.emit(utterance("Where are you?", question.context["session"]))
    assert wait_for(lambda: sms_bodies(phone)) == ["Where are you?"]


def test_watching_flag_reaches_the_handler_through_ovos_core(bus, recorder, service, make_phone):
    """A context key the phone sets survives ovos-core's forward and match messages."""
    phone = make_phone(contacts=[MOM])
    recorder.clear()
    session = Session(f"session-of-{PEER}", pipeline=PIPELINE).serialize()
    bus.emit(utterance("Text Mom are you coming?", session, **{WATCHING: True}))
    assert wait_for(lambda: sms_bodies(phone)) == ["Are you coming?"]
    wait_for(lambda: recorder.of("ovos.utterance.handled"))
    assert recorder.speech() == []


# --- a hub that keeps raw_utterances in the context --------------------------------

class NoRecording:
    """Stands in for the recorded utterances, to show they aren't needed."""

    def record(self, message):
        pass

    def as_said(self, *args, **kwargs):
        raise AssertionError("the recording was consulted")


@pytest.fixture
def raw_hub(service):
    plugin = service.pipeline_plugins[PLUGIN_ID]
    plugin.said = NoRecording()
    return plugin


def raw(text, session):
    return utterance(text, session, **{RAW_UTTERANCES: [text]})


@pytest.mark.parametrize("text,body", [
    ("text Mom saying I'm running late, start without me!", "I'm running late, start without me!"),
    ("Text Mom are you coming?", "Are you coming?"),
])
def test_inline_body_from_raw_utterances(bus, raw_hub, make_phone, text, body):
    phone = make_phone(contacts=[MOM])
    bus.emit(raw(text, Session(f"session-of-{PEER}", pipeline=PIPELINE).serialize()))
    assert wait_for(lambda: sms_bodies(phone)) == [body]


def test_answer_from_raw_utterances(bus, recorder, raw_hub, make_phone):
    phone = make_phone(contacts=[MOM], ask_answer="accept")
    recorder.clear()
    bus.emit(raw("text mom", Session(f"session-of-{PEER}", pipeline=PIPELINE).serialize()))
    question = wait_for(lambda: next((m for m in recorder.speech()
                                      if m.data.get("expect_response")), None))
    bus.emit(raw("Where are you?", question.context["session"]))
    assert wait_for(lambda: sms_bodies(phone)) == ["Where are you?"]
