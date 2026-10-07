"""Tests for waggle.rules, driven by the shared waggle/testdata/rule_cases.json."""
import json
from importlib.resources import files

import pytest

from waggle.messages import Capabilities, Intent, Mode, Rule
from waggle.rules import Decision, decide, decide_for, query_enabled, rule_matches

CASES_DOC = json.loads(files("waggle").joinpath("testdata", "rule_cases.json").read_text("utf-8"))
CASES = CASES_DOC["cases"]

ACTION = "android.intent.action."
CATEGORY = "android.intent.category."
REQUEST_ID = "5f0c1e9a-3b7d-4c4e-9a51-2f8e6b0d7c11"


def _intent(action: str, **kw) -> Intent:
    return Intent(id=REQUEST_ID, action=ACTION + action, **kw)


# --- the shared fixture ------------------------------------------------------

def test_fixture_shape():
    assert CASES_DOC["version"] == 1
    assert isinstance(CASES_DOC["description"], str) and CASES_DOC["description"]
    assert len(CASES) >= 40
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    for c in CASES:
        assert set(c) <= {"name", "rules", "unmatched", "intent", "expect"}, c["name"]
        assert c.get("unmatched", "block") in ("block", "ask"), c["name"]
        assert c["expect"]["mode"] in ("run", "ask", "block"), c["name"]
        idx = c["expect"]["rule"]
        assert idx is None or 0 <= idx < len(c["rules"]), c["name"]
        if idx is not None:
            assert c["rules"][idx]["mode"] == c["expect"]["mode"], c["name"]
        else:
            assert c["expect"]["mode"] == c.get("unmatched", "block"), c["name"]


def _check(decision: Decision, rules: list[Rule], expect: dict) -> None:
    assert decision.mode is Mode(expect["mode"])
    if expect["rule"] is None:
        assert decision.rule is None
    else:
        assert decision.rule is rules[expect["rule"]]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_decide(case):
    rules = [Rule.from_dict(r) for r in case["rules"]]
    intent = Intent.from_dict(case["intent"])
    if "unmatched" in case:
        decision = decide(rules, intent, Mode(case["unmatched"]))
    else:
        decision = decide(rules, intent)  # the default must be block
    _check(decision, rules, case["expect"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_decide_for_capabilities(case):
    caps_data = {"version": 1, "rules": case["rules"]}
    if "unmatched" in case:
        caps_data["unmatched"] = case["unmatched"]
    caps = Capabilities.from_dict(caps_data)
    decision = decide_for(caps, Intent.from_dict(case["intent"]))
    _check(decision, list(caps.rules), case["expect"])


# --- rule_matches ------------------------------------------------------------

def test_rule_matches_action_only():
    rule = Rule(ACTION + "DIAL", Mode.RUN)
    assert rule_matches(rule, _intent("DIAL"))
    assert rule_matches(rule, _intent("DIAL", data="tel:1", package="p", categories=("c",)))
    assert not rule_matches(rule, _intent("SENDTO"))
    assert not rule_matches(Rule(ACTION + "dial", Mode.RUN), _intent("DIAL"))


def test_rule_matches_scheme():
    rule = Rule(ACTION + "DIAL", Mode.RUN, scheme="tel")
    assert rule_matches(rule, _intent("DIAL", data="tel:+15551234567"))
    assert rule_matches(rule, _intent("DIAL", data="TEL:+15551234567"))
    assert not rule_matches(rule, _intent("DIAL", data="sip:me@example.com"))
    assert not rule_matches(rule, _intent("DIAL"))


def test_rule_matches_scheme_parsed_rule_uppercase():
    rule = Rule.from_dict({"action": ACTION + "DIAL", "scheme": "TEL", "mode": "run"})
    assert rule_matches(rule, _intent("DIAL", data="tel:1"))


def test_rule_matches_scheme_constructed_rule_uppercase():
    rule = Rule(ACTION + "DIAL", Mode.RUN, scheme="TEL")
    assert rule_matches(rule, _intent("DIAL", data="tel:1"))
    assert rule_matches(rule, _intent("DIAL", data="Tel:1"))


def test_rule_matches_package():
    rule = Rule(ACTION + "VIEW", Mode.RUN, package="org.mozilla.firefox")
    assert rule_matches(rule, _intent("VIEW", data="https://x/", package="org.mozilla.firefox"))
    assert not rule_matches(rule, _intent("VIEW", data="https://x/", package="com.android.chrome"))
    assert not rule_matches(rule, _intent("VIEW", data="https://x/"))


def test_rule_matches_category():
    rule = Rule(ACTION + "MAIN", Mode.RUN, category=CATEGORY + "LAUNCHER")
    assert rule_matches(rule, _intent("MAIN", categories=(CATEGORY + "LAUNCHER",)))
    assert rule_matches(rule, _intent("MAIN", categories=(CATEGORY + "DEFAULT", CATEGORY + "LAUNCHER")))
    assert not rule_matches(rule, _intent("MAIN", categories=(CATEGORY + "DEFAULT",)))
    assert not rule_matches(rule, _intent("MAIN"))


def test_rule_matches_all_fields():
    rule = Rule(ACTION + "VIEW", Mode.RUN, scheme="https", package="org.mozilla.firefox",
                category=CATEGORY + "BROWSABLE")
    full = dict(data="https://x/", package="org.mozilla.firefox", categories=(CATEGORY + "BROWSABLE",))
    assert rule_matches(rule, _intent("VIEW", **full))
    for key, other in [("data", "http://x/"), ("package", "com.android.chrome"), ("categories", ())]:
        assert not rule_matches(rule, _intent("VIEW", **{**full, key: other})), key


def test_rule_specificity():
    assert Rule("a", Mode.RUN).specificity == 1
    assert Rule("a", Mode.RUN, scheme="tel").specificity == 2
    assert Rule("a", Mode.RUN, scheme="tel", package="p").specificity == 3
    assert Rule("a", Mode.RUN, scheme="tel", package="p", category="c").specificity == 4


# --- decide / Decision -------------------------------------------------------

@pytest.mark.parametrize("mode, allowed", [(Mode.RUN, True), (Mode.ASK, True), (Mode.BLOCK, False)])
def test_decision_allowed(mode, allowed):
    assert Decision(mode).allowed is allowed
    assert Decision(mode, Rule("a", mode)).allowed is allowed


def test_decision_defaults_to_no_rule():
    assert Decision(Mode.BLOCK).rule is None


def test_decide_accepts_any_iterable():
    rules = [Rule(ACTION + "DIAL", Mode.RUN), Rule(ACTION + "DIAL", Mode.ASK, scheme="tel")]
    decision = decide((r for r in rules), _intent("DIAL", data="tel:1"))
    assert decision == Decision(Mode.ASK, rules[1])


def test_decide_unmatched_default_and_string():
    assert decide([], _intent("DIAL")) == Decision(Mode.BLOCK)
    assert decide([], _intent("DIAL"), Mode.ASK) == Decision(Mode.ASK)
    assert decide([], _intent("DIAL"), "ask") == Decision(Mode.ASK)


# --- decide_for / query_enabled on the WAGGLE.md example --------------------

EXAMPLE_CAPABILITIES = {
    "version": 1,
    "client": {"name": "Wiggins", "version": "0.3.0"},
    "timezone": "America/Chicago",
    "lang": "en-US",
    "ask_timeout_s": 15,
    "unmatched": "block",
    "rules": [
        {"action": "android.intent.action.SET_ALARM", "mode": "run"},
        {"action": "android.intent.action.SET_TIMER", "mode": "run"},
        {"action": "android.intent.action.MAIN", "category": "android.intent.category.LAUNCHER", "mode": "run"},
        {"action": "android.intent.action.DIAL", "scheme": "tel", "mode": "ask"},
        {"action": "android.intent.action.SENDTO", "scheme": "smsto", "mode": "ask"},
    ],
    "queries": ["calendar.next", "contacts.lookup", "apps.list"],
}


def test_decide_for_example():
    caps = Capabilities.from_dict(EXAMPLE_CAPABILITIES)
    assert decide_for(caps, _intent("SET_ALARM")) == Decision(Mode.RUN, caps.rules[0])
    assert decide_for(caps, _intent("DIAL", data="tel:1")) == Decision(Mode.ASK, caps.rules[3])
    assert decide_for(caps, _intent("VIEW", data="https://x/")) == Decision(Mode.BLOCK)
    assert not decide_for(caps, _intent("VIEW", data="https://x/")).allowed


def test_decide_for_unmatched_ask():
    caps = Capabilities.from_dict({**EXAMPLE_CAPABILITIES, "unmatched": "ask"})
    assert decide_for(caps, _intent("VIEW", data="https://x/")) == Decision(Mode.ASK)


def test_decide_for_unmatched_absent():
    data = dict(EXAMPLE_CAPABILITIES)
    del data["unmatched"]
    caps = Capabilities.from_dict(data)
    assert decide_for(caps, _intent("VIEW", data="https://x/")) == Decision(Mode.BLOCK)


def test_query_enabled():
    caps = Capabilities.from_dict(EXAMPLE_CAPABILITIES)
    for name in ("calendar.next", "contacts.lookup", "apps.list"):
        assert query_enabled(caps, name)
    assert not query_enabled(caps, "sms.read")
    assert not query_enabled(caps, "")
    some = Capabilities.from_dict({"version": 1, "queries": ["apps.list"]})
    assert query_enabled(some, "apps.list")
    assert not query_enabled(some, "calendar.next")
    assert not query_enabled(Capabilities.from_dict({"version": 1}), "apps.list")
