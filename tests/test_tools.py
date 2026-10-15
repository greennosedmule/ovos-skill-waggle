"""The persona's tools: which ones the asking phone is offered, and running them on the phone."""
from __future__ import annotations

import pytest

from ovos_plugin_manager.persona import find_toolbox_plugins
from ovos_skill_waggle.handlers import WATCHING
from ovos_skill_waggle.requests import REQUEST_RESPONSE
from ovos_skill_waggle.tools import TOOLBOX_ID, Outcome, WaggleToolBox
from waggle.fake_phone import AskAnswer
from waggle.intents import ACTION_DIAL, ACTION_SET_TIMER, ACTION_SHOW_ALARMS
from waggle.messages import APPS_LIST, CALENDAR_NEXT, INTENT

from conftest import NOW, OTHER_PEER, PEER, capabilities, match_message, utterance

ALL_TOOLS = {"set_alarm", "set_timer", "show_alarms", "get_calendar_events", "open_app",
             "call_contact", "compose_text_message", "stop_timers", "dismiss_alarm",
             "snooze_alarm", "navigate_to", "get_phone_status", "set_flashlight", "set_volume",
             "set_ringer", "set_do_not_disturb",
             "get_notifications", "reply_to_notification", "get_phone_location",
             "get_phone_screen", "copy_to_clipboard", "share_text"}
SAM_LEE = {"fn": "Sam Lee", "tel": [{"value": "+15550003333", "type": "cell"}]}
SAM_ORTIZ = {"fn": "Sam Ortiz", "tel": [{"value": "+15550004444", "type": "work"}]}


def persona_query(peer=PEER, text="set a timer for the pasta and text mom"):
    """The persona:query match ovos-core dispatches to ovos-persona for an utterance from ``peer``."""
    utt = utterance(text, peer=peer)
    match = type("Match", (), {"match_type": "persona:query", "skill_id": "persona.openvoiceos",
                               "utterance": text,
                               "match_data": {"utterance": text, "persona": "Claude"}})
    return match_message(utt, match)


def as_persona(message, call):
    """Stands in for ovos-persona's handler, which has the message as an argument."""
    return call()


@pytest.fixture
def toolbox():
    return WaggleToolBox()


def offered(toolbox, message):
    return {tool["name"] for tool in as_persona(message, lambda: toolbox.tool_json_list)}


def test_registered_as_a_toolbox_plugin():
    assert find_toolbox_plugins()[TOOLBOX_ID] is WaggleToolBox


def test_every_request_is_a_tool(toolbox):
    assert set(toolbox.tools) == ALL_TOOLS


def test_phone_is_offered_what_it_allows(toolbox, pipeline, make_phone):
    make_phone()
    assert offered(toolbox, persona_query()) == ALL_TOOLS


def test_phone_is_not_offered_what_it_blocks(toolbox, pipeline, make_phone):
    make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_SET_TIMER, "mode": "block"},
               {"action": ACTION_DIAL, "scheme": "tel", "mode": "ask"}],
        queries=[]))
    # Calls need no contacts query when the number is said; the rest need a rule or a query.
    assert offered(toolbox, persona_query()) == {"call_contact"}


def test_disabled_requests_are_not_offered(toolbox, make_pipeline, make_phone):
    make_pipeline({"enable_timer_set": False})
    make_phone()
    assert offered(toolbox, persona_query()) == ALL_TOOLS - {"set_timer"}


def test_other_satellites_get_no_tools(toolbox, pipeline, make_phone):
    make_phone()
    assert offered(toolbox, persona_query(peer=OTHER_PEER)) == set()
    assert toolbox.tool_json_list == []  # no message on the stack at all


def test_no_tools_without_the_plugin(toolbox):
    assert offered(toolbox, persona_query()) == set()


def test_openai_spec_has_the_arguments(toolbox, pipeline, make_phone):
    make_phone()
    specs = as_persona(persona_query(), lambda: toolbox.openai_tools)
    timer = next(s["function"] for s in specs if s["function"]["name"] == "set_timer")
    assert timer["parameters"]["required"] == ["seconds"]
    assert set(timer["parameters"]["properties"]) == {"seconds", "label"}


def test_tool_runs_on_the_phone(toolbox, bus, recorder, pipeline, make_phone):
    phone = make_phone()
    recorder.clear()
    result = as_persona(persona_query(), lambda: toolbox.call_tool("set_timer", {"seconds": 600}))
    assert result == Outcome(ok=True, summary="Timer set for 10 minutes.")
    assert [e.request.data["action"] for e in phone.log] == [ACTION_SET_TIMER]
    assert recorder.of(INTENT)[0].context["destination"] == PEER
    # The persona says what happened in its own words.
    assert recorder.speech() == []
    assert recorder.of("ovos.utterance.handled") == []
    assert recorder.of(REQUEST_RESPONSE)[0].data["ok"] is True


def test_tool_reports_what_the_phone_refused(toolbox, pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    result = as_persona(persona_query(), lambda: toolbox.call_tool("set_timer", {"seconds": 60}))
    assert not result.ok and result.error == "blocked" and result.summary


def test_ask_rule_prompts_on_the_phone(toolbox, recorder, pipeline, make_phone):
    phone = make_phone(contacts=[SAM_LEE], ask_answer=AskAnswer.ACCEPT)
    recorder.clear()
    result = as_persona(persona_query(), lambda: toolbox.call_tool(
        "compose_text_message", {"name": "Sam Lee", "body": "Running late, sorry!"}))
    assert result.ok
    sent = [e.request.data for e in phone.log if "action" in e.request.data][0]
    assert sent["extras"]["sms_body"]["value"] == "Running late, sorry!"
    assert [m.data["utterance"] for m in recorder.speech()] == ["Confirm on your phone."]


def test_ask_rule_prompt_is_muted_for_a_user_watching(toolbox, recorder, pipeline, make_phone):
    make_phone(contacts=[SAM_LEE], ask_answer=AskAnswer.ACCEPT)
    message = persona_query()
    message.context[WATCHING] = True
    recorder.clear()
    result = as_persona(message, lambda: toolbox.call_tool(
        "compose_text_message", {"name": "Sam Lee", "body": "Running late, sorry!"}))
    assert result.ok
    assert recorder.speech() == []


CAMERA = {"label": "Camera", "package": "app.grapheneos.camera"}


@pytest.mark.parametrize("name,args,on_screen", [
    ("show_alarms", {}, True),
    ("open_app", {"name": "camera"}, True),
    ("call_contact", {"name": "Sam Lee"}, True),
    ("compose_text_message", {"name": "Sam Lee", "body": "Running late"}, True),
    ("set_timer", {"seconds": 60}, False),
    ("set_alarm", {"hour": 7, "minute": 0}, False),
    ("stop_timers", {}, False),
    ("snooze_alarm", {}, False),
    ("dismiss_alarm", {}, False),
    ("get_calendar_events", {}, False),
])
def test_success_says_whether_the_phone_shows_it(toolbox, pipeline, make_phone, name, args,
                                                 on_screen):
    make_phone(contacts=[SAM_LEE], apps=[CAMERA])
    result = as_persona(persona_query(), lambda: toolbox.call_tool(name, args))
    assert result.ok and result.on_screen is on_screen


def test_on_screen_needs_no_watching_user(toolbox, pipeline, make_phone):
    make_phone()
    message = persona_query()
    message.context[WATCHING] = False
    assert as_persona(message, lambda: toolbox.call_tool("show_alarms", {})).on_screen is True


def test_failure_is_never_on_screen(toolbox, pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SHOW_ALARMS, "mode": "block"}]))
    result = as_persona(persona_query(), lambda: toolbox.call_tool("show_alarms", {}))
    assert not result.ok and result.on_screen is False


def test_not_a_phone_is_never_on_screen(toolbox, pipeline):
    result = as_persona(persona_query(peer="not-a-phone"),
                        lambda: toolbox.call_tool("show_alarms", {}))
    assert not result.ok and result.on_screen is False


def test_the_model_reads_no_on_screen(toolbox, pipeline, make_phone):
    """The hub hands the model str(result); on_screen is only for the hub."""
    make_phone()
    result = as_persona(persona_query(), lambda: toolbox.call_tool("show_alarms", {}))
    assert result.on_screen is True
    assert str(result) == "ok=True summary='Here are your alarms.' error=None"


def test_which_one_is_asked_of_the_phone(toolbox, pipeline, make_phone):
    phone = make_phone(contacts=[SAM_LEE, SAM_ORTIZ])
    asked = []

    def ask(origin, dialog, data):
        asked.append((dialog, origin.reply("x").context["destination"]))
        return "Ortiz"

    pipeline.handlers.ask = ask
    result = as_persona(persona_query(), lambda: toolbox.call_tool("call_contact", {"name": "sam"}))
    assert result.ok
    assert asked == [("which_contact", PEER)]
    dialed = [e.request.data for e in phone.log if "action" in e.request.data][0]
    assert dialed["data"] == "tel:+15550004444"


def test_query_tool(toolbox, pipeline, make_phone):
    make_phone(now=NOW, events=[{"summary": "Dentist", "dtstart": "2026-10-08T19:00:00Z",
                                 "dtend": "2026-10-08T20:00:00Z", "all_day": False}])
    result = as_persona(persona_query(), lambda: toolbox.call_tool("get_calendar_events", {}))
    assert result.ok and "Dentist" in result.summary


def test_tool_for_another_satellite_does_nothing(toolbox, recorder, pipeline, make_phone):
    phone = make_phone()
    result = as_persona(persona_query(peer=OTHER_PEER),
                        lambda: toolbox.call_tool("set_timer", {"seconds": 60}))
    assert not result.ok and result.error == "unreachable"
    assert phone.log == []


def test_bad_arguments_are_refused(toolbox, pipeline, make_phone):
    make_phone()
    with pytest.raises(ValueError):
        as_persona(persona_query(), lambda: toolbox.call_tool("set_timer", {"seconds": 0}))
    with pytest.raises(ValueError):
        as_persona(persona_query(), lambda: toolbox.call_tool(
            "get_calendar_events", {"day": "next friday"}))


def test_bus_call(bus, pipeline, make_phone):
    """ToolBox's own bus interface finds the phone from the call message's context."""
    phone = make_phone()
    WaggleToolBox(bus=bus)
    answers = []
    bus.on(f"ovos.persona.tools.{TOOLBOX_ID}.call.response", lambda m: answers.append(m.data))
    call = persona_query().forward(f"ovos.persona.tools.{TOOLBOX_ID}.call",
                                   {"name": "show_alarms", "kwargs": {}})
    bus.emit(call)
    assert answers[0]["result"]["ok"] is True
    assert phone.log and phone.log[0].request.data["action"].endswith("SHOW_ALARMS")


def test_query_names_used():
    # The tool list judges calendar and app tools by these queries being shared.
    assert {CALENDAR_NEXT, APPS_LIST} <= set(capabilities().queries)
