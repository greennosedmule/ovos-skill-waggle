"""The persona's tools end to end: ovos-core's IntentService, ovos-persona and ovos-native-toolcall-loop.

An utterance the Waggle stage doesn't parse falls through to the persona.
Its solver is ovos-agentic-loop's tool-calling loop with this package's
toolbox, around a scripted model that calls a tool and then answers with
what the tool returned. This runs the real call stack, so it checks that the
tools find the asking phone from ovos-persona's handler.

With ovos-plugin-manager 2.12.5a1 the loop can't load its model from its
``brain`` setting (it calls ``load_chat_plugin`` with a ``config`` argument
that function doesn't take), so the scripted loop sets its brain itself, as
a hub has to.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

pytest.importorskip("ovos_core")
pytest.importorskip("ovos_persona")
pytest.importorskip("ovos_agentic_loop")

import ovos_persona  # noqa: E402
import ovos_persona.solvers  # noqa: E402
from ovos_agentic_loop.native_toolcall import NativeToolCallEngine  # noqa: E402
from ovos_core.intent_services.service import IntentService  # noqa: E402
from ovos_plugin_manager.templates.agent_tools import ToolBox  # noqa: E402
from ovos_plugin_manager.templates.agents import (  # noqa: E402
    AgentMessage, ChatEngine, MessageRole, ToolCall,
)

from ovos_skill_waggle.pipeline import PLUGIN_ID  # noqa: E402
from ovos_skill_waggle.tools import TOOLBOX_ID  # noqa: E402
from waggle.intents import ACTION_DIAL, ACTION_SET_TIMER  # noqa: E402

from conftest import NOW, OTHER_PEER, PEER, TestPipeline  # noqa: E402

PERSONA_PLUGIN = "ovos-persona-pipeline-plugin"
LOOP = "test-toolcall-loop"
PIPELINE = [
    "ovos-stop-pipeline-plugin-high",
    "ovos-converse-pipeline-plugin",
    f"{PLUGIN_ID}-high",
    f"{PERSONA_PLUGIN}-low",
]
HANDLED = "ovos.utterance.handled"
# Nothing the Waggle stage takes, so it reaches the persona.
ASK = "could you help me with the pasta, it needs ten minutes"
SAM_LEE = {"fn": "Sam Lee", "tel": [{"value": "+15550003333", "type": "cell"}]}
SAM_ORTIZ = {"fn": "Sam Ortiz", "tel": [{"value": "+15550004444", "type": "work"}]}


class ScriptedModel(ChatEngine):
    """Calls the scripted tool on the user's turn, then answers with the tool's result."""
    supports_tools = True
    call: tuple = ("set_timer", {"seconds": 600, "label": "pasta"})
    offered: list = []

    def continue_chat(self, messages, session_id="default", lang=None, units=None, tools=None):
        offered = [t["function"]["name"] for t in ToolBox.normalize_tools(tools)]
        ScriptedModel.offered.append(offered)
        last = messages[-1]
        if last.role == MessageRole.TOOL:
            return AgentMessage(MessageRole.ASSISTANT, f"Done. {last.content}")
        name, arguments = self.call
        if name not in offered:
            return AgentMessage(MessageRole.ASSISTANT, "I can't do that on your phone.")
        return AgentMessage(MessageRole.ASSISTANT, "",
                            tool_calls=[ToolCall(str(uuid.uuid4()), name, dict(arguments))])


class ScriptedLoop(NativeToolCallEngine):
    def _load_brain(self):
        return ScriptedModel()


@pytest.fixture
def service(bus, tmp_path, monkeypatch):
    plugins = {LOOP: ScriptedLoop}
    monkeypatch.setattr(ovos_persona, "get_utterance_handler_plugins", lambda: plugins)
    monkeypatch.setattr(ovos_persona.solvers, "get_utterance_handler_plugins", lambda: plugins)
    ScriptedModel.offered = []
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "claude.json").write_text(json.dumps({
        "name": "Claude", "solvers": [LOOP], LOOP: {"toolboxes": [TOOLBOX_ID]}}))

    service = IntentService(bus, config={})
    waggle = service.pipeline_plugins[PLUGIN_ID]
    waggle.__class__ = TestPipeline
    waggle.clock = lambda: NOW
    waggle.waggle_settings = waggle.waggle_settings.__class__.from_config(
        {"response_timeout_s": 0.5})
    # The hub's persona settings (mycroft.conf intents.persona), with the scripted persona.
    service.pipeline_plugins[PERSONA_PLUGIN].default_shutdown()
    persona = ovos_persona.PersonaService(bus, {
        "handle_fallback": True, "default_persona": "Claude",
        "personas_path": str(personas), "ignore_plugin_personas": True})
    service.pipeline_plugins[PERSONA_PLUGIN] = persona
    yield service
    service.shutdown()
    waggle.default_shutdown()
    persona.default_shutdown()


def say(bus, text, peer, session=None):
    session = session or Session(f"session-of-{peer}", pipeline=PIPELINE).serialize()
    bus.emit(Message("recognizer_loop:utterance", {"utterances": [text], "lang": "en-US"},
                     {"source": peer, "peer": peer, "destination": "skills", "session": session}))


def spoken(recorder):
    return [m.data["utterance"] for m in recorder.speech()]


def test_persona_sets_a_timer_on_the_phone(bus, recorder, service, make_phone):
    phone = make_phone()
    recorder.clear()
    say(bus, ASK, PEER)

    assert [e.request.data["action"] for e in phone.log] == [ACTION_SET_TIMER]
    assert phone.log[0].request.data["extras"]["android.intent.extra.alarm.LENGTH"]["value"] == 600
    assert set(ScriptedModel.offered[0]) == {
        "set_alarm", "set_timer", "show_alarms", "get_calendar_events", "open_app",
        "call_contact", "compose_text_message", "stop_timers", "dismiss_alarm",
        "snooze_alarm"}
    # Waggle stays quiet; the persona says what happened.
    assert spoken(recorder) == ["Done. ok=True summary='pasta timer set for 10 minutes.' error=None"]
    assert recorder.speech()[0].context["destination"] == PEER
    handled = recorder.of(HANDLED)
    assert len(handled) == 1 and handled[0].context["destination"] == PEER


def test_other_satellite_gets_no_phone_tools(bus, recorder, service, make_phone):
    phone = make_phone()
    recorder.clear()
    say(bus, ASK, OTHER_PEER)
    assert ScriptedModel.offered == [[]]
    assert phone.log == []
    assert spoken(recorder) == ["I can't do that on your phone."]


def test_follow_up_question_from_a_tool(bus, recorder, service, make_phone):
    """The persona's call asks "Which Sam?"; the answer comes back through the converse stage."""
    phone = make_phone(contacts=[SAM_LEE, SAM_ORTIZ])
    ScriptedModel.call = ("call_contact", {"name": "sam"})
    try:
        recorder.clear()
        first = threading.Thread(target=say, args=(bus, "ring up sam for me", PEER), daemon=True)
        first.start()
        question = _wait_for_question(recorder)
        assert question.data["utterance"] == "Which Sam: Sam Lee or Sam Ortiz?"
        assert question.context["destination"] == PEER
        say(bus, "Ortiz", PEER, session=question.context["session"])
        first.join(10)
        assert not first.is_alive()
    finally:
        ScriptedModel.call = ("set_timer", {"seconds": 600, "label": "pasta"})

    dialed = [e.request.data for e in phone.log if e.request.data.get("action") == ACTION_DIAL]
    assert [d["data"] for d in dialed] == ["tel:+15550004444"]
    assert spoken(recorder)[-1].startswith("Done. ok=True")
    # The answer is taken by the waiting tool, so one end marker closes both utterances.
    handled = recorder.of(HANDLED)
    assert len(handled) == 1 and handled[0].context["destination"] == PEER


def _wait_for_question(recorder, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        question = next((m for m in recorder.speech() if m.data.get("expect_response")), None)
        if question is not None:
            return question
        time.sleep(0.05)
    raise AssertionError(recorder.types())
