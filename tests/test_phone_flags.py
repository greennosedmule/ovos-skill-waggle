"""The per-utterance flags can_ask and phone_actions (WAGGLE.md "Common rules"), and cannot_ask."""
from __future__ import annotations

import pytest

from ovos_skill_waggle.capabilities import CAN_ASK, PHONE_ACTIONS, as_heard
from waggle.intents import ACTION_SET_TIMER
from waggle.messages import INTENT, Capabilities, ErrorCode, Mode, Rule
from waggle.rules import limited

from conftest import capabilities, utterance
from test_handlers import request, run, spoken
from test_tools import ALL_TOOLS, offered, persona_query

# The fake phone's default rules ask before these four.
ASKING_TOOLS = {"dismiss_alarm", "call_contact", "compose_text_message",
                "reply_to_notification"}
TIMER = ("timer.set", {"seconds": 600})


def flagged(message, **flags):
    message.context.update(flags)
    return message


def ask_timer():
    return capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "ask"}], ask_timeout_s=0.3)


# --- the library -------------------------------------------------------------

CAPS = Capabilities(version=1, unmatched=Mode.ASK, queries=frozenset({"apps.list"}),
                    rules=(Rule("a.RUN", Mode.RUN), Rule("a.ASK", Mode.ASK, scheme="tel"),
                           Rule("a.BLOCK", Mode.BLOCK)))


def test_limited_without_flags_is_unchanged():
    assert limited(CAPS) is CAPS


def test_limited_without_asking():
    caps = limited(CAPS, can_ask=False)
    assert [r.mode for r in caps.rules] == [Mode.RUN, Mode.BLOCK, Mode.BLOCK]
    assert caps.rules[1].scheme == "tel"
    assert caps.unmatched is Mode.BLOCK and caps.queries == CAPS.queries


def test_limited_without_actions():
    caps = limited(CAPS, actions=False, can_ask=True)
    assert {r.mode for r in caps.rules} == {Mode.BLOCK}
    assert caps.unmatched is Mode.BLOCK and caps.queries == frozenset()


# --- reading the flags ---------------------------------------------------------

@pytest.mark.parametrize("value", [None, True, "false", 0, [], "no"])
def test_only_false_counts(pipeline, make_phone, value):
    make_phone()
    phone = pipeline.capabilities.get(utterance("x").context["peer"])
    message = flagged(utterance("x"), **{CAN_ASK: value, PHONE_ACTIONS: value})
    assert as_heard(phone, message) is phone


def test_absent_flags_keep_the_phone(pipeline, make_phone):
    make_phone()
    phone = pipeline.capabilities.get(utterance("x").context["peer"])
    assert as_heard(phone, utterance("x")) is phone
    assert as_heard(phone, None) is phone


# --- the stage -----------------------------------------------------------------

def match(pipeline, text, **flags):
    utt = flagged(utterance(text), **flags)
    return pipeline.match_high(utt.data["utterances"], "en-US", utt)


def test_stage_takes_an_ask_rule_by_default(pipeline, make_phone):
    make_phone(capabilities=ask_timer())
    assert match(pipeline, "set a timer for 10 minutes") is not None


def test_stage_steps_aside_for_an_ask_rule_it_cant_ask(pipeline, make_phone):
    make_phone(capabilities=ask_timer())
    assert match(pipeline, "set a timer for 10 minutes", **{CAN_ASK: False}) is None


def test_stage_still_takes_a_run_rule_it_cant_ask(pipeline, make_phone):
    make_phone()
    assert match(pipeline, "set a timer for 10 minutes", **{CAN_ASK: False}) is not None


def test_stage_steps_aside_without_phone_actions(pipeline, make_phone):
    make_phone()
    assert match(pipeline, "set a timer for 10 minutes", **{PHONE_ACTIONS: False}) is None
    assert match(pipeline, "set a timer for 10 minutes", **{PHONE_ACTIONS: True}) is not None


# --- the persona's tools ---------------------------------------------------------

@pytest.fixture
def toolbox():
    from ovos_skill_waggle.tools import WaggleToolBox
    return WaggleToolBox()


def test_tools_without_flags(toolbox, pipeline, make_phone):
    make_phone()
    assert offered(toolbox, persona_query()) == ALL_TOOLS


def test_tools_without_asking(toolbox, pipeline, make_phone):
    make_phone()
    assert offered(toolbox, flagged(persona_query(), **{CAN_ASK: False})) == \
        ALL_TOOLS - ASKING_TOOLS


def test_no_tools_without_phone_actions(toolbox, pipeline, make_phone):
    make_phone()
    assert offered(toolbox, flagged(persona_query(), **{PHONE_ACTIONS: False})) == set()


# --- the handlers ----------------------------------------------------------------

def test_handler_cant_ask(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=ask_timer())
    response = run(bus, recorder, flagged(request(*TIMER), **{CAN_ASK: False}))
    assert response.error == "cannot_ask"
    assert spoken(recorder) == ["Your phone can't ask you to confirm that right now."]
    assert recorder.of(INTENT) == []  # nothing sent, so no "Confirm on your phone."


def test_handler_without_phone_actions(bus, recorder, phone):
    response = run(bus, recorder, flagged(request(*TIMER), **{PHONE_ACTIONS: False}))
    assert response.error == "blocked"
    assert spoken(recorder) == ["Your phone isn't taking requests right now."]
    assert recorder.of(INTENT) == []


def test_handler_query_without_phone_actions(bus, recorder, pipeline, make_phone):
    make_phone(apps=[{"label": "Camera", "package": "app.grapheneos.camera"}])
    response = run(bus, recorder, flagged(request("app.open", {"name": "camera"}),
                                          **{PHONE_ACTIONS: False}))
    assert response.error == "blocked"
    assert spoken(recorder) == ["Your phone isn't taking requests right now."]


def test_a_rule_that_blocks_still_says_so(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    response = run(bus, recorder, flagged(request(*TIMER), **{CAN_ASK: False}))
    assert response.error == "blocked"
    assert spoken(recorder) == ["Your phone doesn't allow that."]


def test_a_run_rule_runs_when_it_cant_ask(bus, recorder, phone):
    response = run(bus, recorder, flagged(request(*TIMER), **{CAN_ASK: False}))
    assert response.ok and spoken(recorder) == ["Timer set for 10 minutes."]


def test_an_ask_rule_asks_by_default(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=ask_timer())
    response = run(bus, recorder, request(*TIMER))
    assert response.ok
    assert spoken(recorder) == ["Confirm on your phone.", "Timer set for 10 minutes."]


def test_phone_answers_cannot_ask(bus, recorder, pipeline, make_phone):
    """The phone's own backstop, for a hub that didn't know."""
    make_phone(launch=ErrorCode.CANNOT_ASK)
    response = run(bus, recorder, request(*TIMER))
    assert response.error == "cannot_ask"
    assert spoken(recorder) == ["Your phone can't ask you to confirm that right now."]
