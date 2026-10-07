"""Tests for waggle.messages: models, validation and wire helpers (WAGGLE.md v1)."""
import copy
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from waggle.messages import (
    PROTOCOL_VERSION, SUPPORTED_VERSIONS, Capabilities, ClientInfo, ErrorCode, Extra, Intent, Mode,
    Query, Response, Rule, WaggleError, from_wire_time, new_request_id, to_wire_time, uri_scheme,
)

BAD = ErrorCode.BAD_REQUEST


def raises(code: ErrorCode, fn, *args, **kwargs) -> WaggleError:
    with pytest.raises(WaggleError) as info:
        fn(*args, **kwargs)
    assert info.value.code is code, info.value.message
    return info.value


# --- WAGGLE.md examples, verbatim -------------------------------------------

CAPABILITIES_EXAMPLE = {
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

INTENT_EXAMPLE = {
    "id": "5f0c1e9a-3b7d-4c4e-9a51-2f8e6b0d7c11",
    "description": "Set an alarm for 6:30 AM",
    "action": "android.intent.action.SET_ALARM",
    "extras": {
        "android.intent.extra.alarm.HOUR": {"type": "int", "value": 6},
        "android.intent.extra.alarm.MINUTES": {"type": "int", "value": 30},
        "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": True},
    },
}

QUERY_EXAMPLE = {"id": "c2a1...", "name": "contacts.lookup", "params": {"name": "Mom"}}

CALENDAR_DATA = {"events": [
    {"summary": "Dentist", "dtstart": "2026-10-05T19:00:00Z", "dtend": "2026-10-05T20:00:00Z",
     "all_day": False, "location": "123 Main St", "calendar": "Personal"}
]}
CONTACTS_DATA = {"contacts": [
    {"fn": "Mom", "tel": [{"value": "+15551234567", "type": "cell"}]}
]}
APPS_DATA = {"apps": [{"label": "Camera", "package": "app.grapheneos.camera"}]}

ID = INTENT_EXAMPLE["id"]
FORBIDDEN = ["content", "file", "intent", "android-app"]


def intent_data(**kw) -> dict:
    return {"id": ID, "action": "android.intent.action.VIEW", **kw}


# --- constants and WaggleError ----------------------------------------------

def test_protocol_version():
    assert PROTOCOL_VERSION == 1
    assert SUPPORTED_VERSIONS == {1}


def test_waggle_error():
    e = WaggleError("blocked", "nope")
    assert e.code is ErrorCode.BLOCKED
    assert e.message == "nope" and str(e) == "nope"
    assert isinstance(e, ValueError)
    with pytest.raises(ValueError):
        WaggleError("not_a_code", "x")


# --- capabilities ------------------------------------------------------------

def test_capabilities_example_parses():
    caps = Capabilities.from_dict(CAPABILITIES_EXAMPLE)
    assert caps.version == 1
    assert caps.client == ClientInfo("Wiggins", "0.3.0")
    assert caps.timezone == "America/Chicago"
    assert caps.lang == "en-US"
    assert caps.ask_timeout_s == 15
    assert caps.unmatched is Mode.BLOCK
    assert caps.queries == {"calendar.next", "contacts.lookup", "apps.list"}
    assert caps.rules[2] == Rule("android.intent.action.MAIN", Mode.RUN,
                                 category="android.intent.category.LAUNCHER")
    assert caps.rules[3] == Rule("android.intent.action.DIAL", Mode.ASK, scheme="tel")
    assert len(caps.rules) == 5


def test_capabilities_round_trip():
    caps = Capabilities.from_dict(CAPABILITIES_EXAMPLE)
    out = caps.to_dict()
    assert Capabilities.from_dict(out) == caps
    # queries is a set, so its order on the wire is not preserved
    assert sorted(out.pop("queries")) == sorted(CAPABILITIES_EXAMPLE["queries"])
    expected = {k: v for k, v in CAPABILITIES_EXAMPLE.items() if k != "queries"}
    assert out == expected


def test_capabilities_minimal_defaults():
    caps = Capabilities.from_dict({"version": 1})
    assert caps == Capabilities(version=1)
    assert caps.rules == () and caps.queries == frozenset()
    assert caps.unmatched is Mode.BLOCK
    assert caps.timezone is caps.lang is caps.ask_timeout_s is caps.client is None
    assert caps.to_dict() == {"version": 1, "unmatched": "block", "rules": [], "queries": []}


def test_capabilities_unknown_fields_ignored():
    data = copy.deepcopy(CAPABILITIES_EXAMPLE)
    data["battery"] = 80
    data["client"]["build"] = 42
    data["rules"][0]["note"] = "morning"
    caps = Capabilities.from_dict(data)
    assert caps == Capabilities.from_dict(CAPABILITIES_EXAMPLE)
    assert "battery" not in caps.to_dict()


def test_capabilities_unmatched_absent_defaults_to_block():
    data = {k: v for k, v in CAPABILITIES_EXAMPLE.items() if k != "unmatched"}
    assert Capabilities.from_dict(data).unmatched is Mode.BLOCK


def test_capabilities_unmatched_ask():
    assert Capabilities.from_dict({"version": 1, "unmatched": "ask"}).unmatched is Mode.ASK


@pytest.mark.parametrize("unmatched", ["run", "allow", "BLOCK", "", None, 0, True, ["block"]])
def test_capabilities_bad_unmatched(unmatched):
    raises(BAD, Capabilities.from_dict, {"version": 1, "unmatched": unmatched})


def test_capabilities_unsupported_version():
    raises(ErrorCode.UNSUPPORTED_VERSION, Capabilities.from_dict, {**CAPABILITIES_EXAMPLE, "version": 2})
    raises(ErrorCode.UNSUPPORTED_VERSION, Capabilities.from_dict, {"version": 0})


@pytest.mark.parametrize("version", ["1", True, False, 1.0, None, [1]])
def test_capabilities_bad_version(version):
    raises(BAD, Capabilities.from_dict, {**CAPABILITIES_EXAMPLE, "version": version})


def test_capabilities_missing_version():
    data = {k: v for k, v in CAPABILITIES_EXAMPLE.items() if k != "version"}
    raises(BAD, Capabilities.from_dict, data)


@pytest.mark.parametrize("data", [None, [], "caps", 1])
def test_capabilities_not_an_object(data):
    raises(BAD, Capabilities.from_dict, data)


@pytest.mark.parametrize("timeout", [0, 15, 2.5])
def test_capabilities_ask_timeout_ok(timeout):
    assert Capabilities.from_dict({"version": 1, "ask_timeout_s": timeout}).ask_timeout_s == timeout


@pytest.mark.parametrize("timeout", [-1, -0.5, "15", True, False, [15], {}])
def test_capabilities_bad_ask_timeout(timeout):
    raises(BAD, Capabilities.from_dict, {"version": 1, "ask_timeout_s": timeout})


@pytest.mark.parametrize("rules", [{}, "rules", {"action": "a", "mode": "run"}])
def test_capabilities_rules_not_a_list(rules):
    raises(BAD, Capabilities.from_dict, {"version": 1, "rules": rules})


@pytest.mark.parametrize("queries", ["apps.list", {"apps.list": True}, ["apps.list", 3], [None]])
def test_capabilities_bad_queries(queries):
    raises(BAD, Capabilities.from_dict, {"version": 1, "queries": queries})


def test_capabilities_unknown_queries_kept():
    caps = Capabilities.from_dict({"version": 1, "queries": ["apps.list", "future.query"]})
    assert caps.queries == {"apps.list", "future.query"}


@pytest.mark.parametrize("key", ["timezone", "lang"])
def test_capabilities_bad_string_fields(key):
    raises(BAD, Capabilities.from_dict, {"version": 1, key: 5})


@pytest.mark.parametrize("client", ["Wiggins", {"version": "1"}, {"name": 3}, None])
def test_capabilities_malformed_client_dropped(client):
    assert Capabilities.from_dict({"version": 1, "client": client}).client is None


def test_capabilities_client_without_version():
    caps = Capabilities.from_dict({"version": 1, "client": {"name": "Wiggins", "version": 3}})
    assert caps.client == ClientInfo("Wiggins")
    assert caps.to_dict()["client"] == {"name": "Wiggins"}


def test_capabilities_bad_rule_propagates():
    raises(BAD, Capabilities.from_dict, {"version": 1, "rules": [{"action": "a", "mode": "allow"}]})
    raises(BAD, Capabilities.from_dict, {"version": 1, "rules": ["a"]})


# --- rules -------------------------------------------------------------------

@pytest.mark.parametrize("rule", CAPABILITIES_EXAMPLE["rules"])
def test_rule_round_trip_example(rule):
    assert Rule.from_dict(rule).to_dict() == rule


def test_rule_round_trip_all_fields():
    data = {"action": "android.intent.action.VIEW", "scheme": "https", "package": "org.mozilla.firefox",
            "category": "android.intent.category.BROWSABLE", "mode": "block"}
    rule = Rule.from_dict(data)
    assert rule == Rule("android.intent.action.VIEW", Mode.BLOCK, "https", "org.mozilla.firefox",
                        "android.intent.category.BROWSABLE")
    assert rule.to_dict() == data
    assert Rule.from_dict(rule.to_dict()) == rule


def test_rule_scheme_lowercased():
    rule = Rule.from_dict({"action": "a", "scheme": "TEL", "mode": "run"})
    assert rule.scheme == "tel"
    assert rule.to_dict()["scheme"] == "tel"
    assert Rule("a", Mode.RUN, scheme="TEL").scheme == "tel"


def test_rule_unknown_fields_ignored():
    assert Rule.from_dict({"action": "a", "mode": "ask", "extra": 1}) == Rule("a", Mode.ASK)


@pytest.mark.parametrize("mode", [None, "allow", "RUN", "", 1, True])
def test_rule_bad_mode(mode):
    raises(BAD, Rule.from_dict, {"action": "a", "mode": mode})


def test_rule_missing_mode():
    raises(BAD, Rule.from_dict, {"action": "a"})


@pytest.mark.parametrize("data", [{"mode": "run"}, {"action": "", "mode": "run"},
                                  {"action": None, "mode": "run"}, {"action": 5, "mode": "run"}])
def test_rule_bad_action(data):
    raises(BAD, Rule.from_dict, data)


@pytest.mark.parametrize("key", ["scheme", "package", "category"])
def test_rule_bad_optional_field(key):
    raises(BAD, Rule.from_dict, {"action": "a", "mode": "run", key: 1})


@pytest.mark.parametrize("data", [None, [], "rule"])
def test_rule_not_an_object(data):
    raises(BAD, Rule.from_dict, data)


# --- intents -----------------------------------------------------------------

def test_intent_example_round_trip():
    intent = Intent.from_dict(INTENT_EXAMPLE)
    assert intent.id == ID
    assert intent.action == "android.intent.action.SET_ALARM"
    assert intent.description == "Set an alarm for 6:30 AM"
    assert intent.extras["android.intent.extra.alarm.HOUR"] == Extra("int", 6)
    assert intent.extras["android.intent.extra.alarm.SKIP_UI"] == Extra("bool", True)
    assert intent.data is None and intent.scheme is None and intent.categories == ()
    assert intent.to_dict() == INTENT_EXAMPLE
    assert Intent.from_dict(intent.to_dict()) == intent


def test_intent_all_fields_round_trip():
    data = intent_data(
        description="Open the docs", data="https://example.com/docs", mime_type="text/html",
        categories=["android.intent.category.BROWSABLE", "android.intent.category.DEFAULT"],
        package="org.mozilla.firefox",
        extras={
            "i": {"type": "int", "value": -7}, "l": {"type": "long", "value": 2**40},
            "f": {"type": "float", "value": 1.5}, "d": {"type": "double", "value": 2},
            "b": {"type": "bool", "value": False}, "s": {"type": "string", "value": ""},
            "sa": {"type": "string[]", "value": ["a", "b"]}, "u": {"type": "uri", "value": "geo:0,0"},
        })
    intent = Intent.from_dict(data)
    assert intent.categories == ("android.intent.category.BROWSABLE", "android.intent.category.DEFAULT")
    assert intent.scheme == "https"
    assert intent.to_dict() == data
    assert Intent.from_dict(intent.to_dict()) == intent


def test_intent_minimal():
    intent = Intent.from_dict({"id": "x", "action": "a"})
    assert intent.to_dict() == {"id": "x", "action": "a"}


def test_intent_empty_categories_and_extras_omitted():
    assert Intent.from_dict({"id": "x", "action": "a", "categories": [], "extras": {}}).to_dict() == \
        {"id": "x", "action": "a"}


def test_intent_unknown_fields_ignored():
    data = copy.deepcopy(INTENT_EXAMPLE)
    data["flags"] = 268435456
    data["component"] = "com.evil/.Activity"
    data["extras"]["android.intent.extra.alarm.HOUR"]["note"] = "x"
    intent = Intent.from_dict(data)
    assert intent == Intent.from_dict(INTENT_EXAMPLE)
    assert intent.to_dict() == INTENT_EXAMPLE


@pytest.mark.parametrize("data", [None, [], "intent"])
def test_intent_not_an_object(data):
    raises(BAD, Intent.from_dict, data)


@pytest.mark.parametrize("key", ["id", "action"])
@pytest.mark.parametrize("value", ["", None, 5, ["x"]])
def test_intent_bad_required_field(key, value):
    raises(BAD, Intent.from_dict, {**INTENT_EXAMPLE, key: value})


@pytest.mark.parametrize("key", ["id", "action"])
def test_intent_missing_required_field(key):
    raises(BAD, Intent.from_dict, {k: v for k, v in INTENT_EXAMPLE.items() if k != key})


def test_intent_direct_construction_validates():
    raises(BAD, Intent, id="", action="a")
    raises(BAD, Intent, id="x", action="")
    raises(BAD, Intent, id="x", action="a", data="content://x")
    raises(BAD, Intent, id="x", action="a", extras={"e": Extra("int", 1.5)})
    raises(BAD, Intent, id="x", action="a", extras={"": Extra("int", 1)})


@pytest.mark.parametrize("key", ["description", "data", "mime_type", "package"])
def test_intent_bad_optional_string(key):
    raises(BAD, Intent.from_dict, intent_data(**{key: 5}))


@pytest.mark.parametrize("scheme", FORBIDDEN + [s.upper() for s in FORBIDDEN] + ["Content", "File"])
def test_intent_forbidden_data_scheme(scheme):
    raises(BAD, Intent.from_dict, intent_data(data=f"{scheme}://com.example/x"))


@pytest.mark.parametrize("data", ["intent:#Intent;action=android.intent.action.VIEW;end",
                                  "file:/sdcard/x", "content:x"])
def test_intent_forbidden_data_scheme_opaque(data):
    raises(BAD, Intent.from_dict, intent_data(data=data))


@pytest.mark.parametrize("data", ["example.com/page", "//example.com/page", "/sdcard/x", "+15551234567", ""])
def test_intent_data_without_scheme(data):
    raises(BAD, Intent.from_dict, intent_data(data=data))


@pytest.mark.parametrize("data", ["tel:+15551234567", "smsto:+15551234567", "https://example.com/",
                                  "geo:0,0?q=coffee", "mailto:a@example.com", "HTTPS://EXAMPLE.COM/"])
def test_intent_allowed_data(data):
    assert Intent.from_dict(intent_data(data=data)).data == data


@pytest.mark.parametrize("where", ["data", "extra"])
def test_intent_malformed_uri_does_not_crash(where):
    # Waggle checks only the scheme; the rest of the URI is the target app's business.
    uri = "http://[::1/x"
    if where == "data":
        data = intent_data(data=uri)
    else:
        data = intent_data(extras={"u": {"type": "uri", "value": uri}})
    Intent.from_dict(data)


@pytest.mark.parametrize("categories", ["android.intent.category.LAUNCHER", {"c": 1}, 5])
def test_intent_categories_not_a_list(categories):
    raises(BAD, Intent.from_dict, intent_data(categories=categories))


@pytest.mark.parametrize("categories", [["a", 5], [""], [None], [["a"]]])
def test_intent_bad_category_items(categories):
    raises(BAD, Intent.from_dict, intent_data(categories=categories))


@pytest.mark.parametrize("extras", [[], "x", 5])
def test_intent_extras_not_an_object(extras):
    raises(BAD, Intent.from_dict, intent_data(extras=extras))


@pytest.mark.parametrize("extra", [5, "int", [], None])
def test_intent_extra_not_an_object(extra):
    raises(BAD, Intent.from_dict, intent_data(extras={"e": extra}))


def test_intent_scheme_property():
    assert Intent.from_dict(intent_data(data="TEL:+1")).scheme == "tel"
    assert Intent.from_dict(intent_data()).scheme is None


# --- extras ------------------------------------------------------------------

VALID_EXTRAS = [
    ("int", 0), ("int", 6), ("int", -2**31), ("int", 2**31 - 1),
    ("long", 0), ("long", 2**31), ("long", -2**31 - 1), ("long", 2**63 - 1), ("long", -2**63),
    ("float", 1.5), ("float", 2), ("float", 0), ("float", -1e10),
    ("double", 1.5), ("double", 3), ("double", -0.25),
    ("bool", True), ("bool", False),
    ("string", ""), ("string", "hello"),
    ("string[]", []), ("string[]", ["a", "b"]), ("string[]", [""]),
    ("uri", "https://example.com/"), ("uri", "tel:+15551234567"), ("uri", "GEO:0,0"),
]

INVALID_EXTRAS = [
    ("int", True), ("int", False), ("int", 2**31), ("int", -2**31 - 1), ("int", 1.0), ("int", 1.5),
    ("int", "1"), ("int", None),
    ("long", True), ("long", 2**63), ("long", -2**63 - 1), ("long", 1.0), ("long", "1"), ("long", None),
    ("float", True), ("float", "1.5"), ("float", None), ("float", [1.5]),
    ("double", False), ("double", "1"), ("double", None),
    ("bool", 1), ("bool", 0), ("bool", "true"), ("bool", None),
    ("string", 1), ("string", None), ("string", ["a"]), ("string", True),
    ("string[]", "a"), ("string[]", ["a", 1]), ("string[]", [None]), ("string[]", [["a"]]),
    ("string[]", None), ("string[]", {"a": "b"}),
    ("uri", 1), ("uri", None), ("uri", ""), ("uri", "example.com/x"), ("uri", ["https://x/"]),
]


@pytest.mark.parametrize("type_, value", VALID_EXTRAS)
def test_extra_valid(type_, value):
    extra = Extra.from_dict("e", {"type": type_, "value": value})
    assert extra == Extra(type_, value)
    assert extra.to_dict() == {"type": type_, "value": value}
    assert Intent.from_dict(intent_data(extras={"e": extra.to_dict()})).extras["e"] == extra


@pytest.mark.parametrize("type_, value", INVALID_EXTRAS)
def test_extra_invalid(type_, value):
    raises(BAD, Extra.from_dict, "e", {"type": type_, "value": value})
    raises(BAD, Intent.from_dict, intent_data(extras={"e": {"type": type_, "value": value}}))
    raises(BAD, Extra(type_, value).validate)


@pytest.mark.parametrize("type_", ["short", "char", "Int", "STRING", "uri[]", "int[]", "", None, 1])
def test_extra_unknown_type(type_):
    raises(BAD, Extra.from_dict, "e", {"type": type_, "value": 1})


def test_extra_missing_type():
    raises(BAD, Extra.from_dict, "e", {"value": 1})


@pytest.mark.parametrize("scheme", FORBIDDEN + [s.upper() for s in FORBIDDEN])
def test_uri_extra_forbidden_scheme(scheme):
    uri = f"{scheme}://com.example/x"
    raises(BAD, Extra.from_dict, "e", {"type": "uri", "value": uri})
    raises(BAD, Intent.from_dict, intent_data(extras={"e": {"type": "uri", "value": uri}}))


def test_string_extra_may_hold_forbidden_scheme_text():
    # Only uri extras are scheme-checked; a string extra is just text.
    assert Extra.from_dict("e", {"type": "string", "value": "content://x"}).value == "content://x"


# --- queries -----------------------------------------------------------------

def test_query_example_round_trip():
    query = Query.from_dict(QUERY_EXAMPLE)
    assert query == Query("c2a1...", "contacts.lookup", {"name": "Mom"})
    assert query.to_dict() == QUERY_EXAMPLE
    assert Query.from_dict(query.to_dict()) == query


def test_query_unknown_fields_ignored():
    query = Query.from_dict({**QUERY_EXAMPLE, "priority": "high"})
    assert query.to_dict() == QUERY_EXAMPLE


def test_query_unknown_params_ignored():
    query = Query.from_dict({"id": "q", "name": "apps.list", "params": {"sort": "label"}})
    assert query.params == {"sort": "label"}


def test_query_params_absent():
    query = Query.from_dict({"id": "q", "name": "calendar.next"})
    assert query.params == {}
    assert query.to_dict() == {"id": "q", "name": "calendar.next", "params": {}}


@pytest.mark.parametrize("data", [None, [], "query"])
def test_query_not_an_object(data):
    raises(BAD, Query.from_dict, data)


@pytest.mark.parametrize("data", [
    {"name": "apps.list"}, {"id": "", "name": "apps.list"}, {"id": 5, "name": "apps.list"},
    {"id": "q"}, {"id": "q", "name": ""}, {"id": "q", "name": 5},
])
def test_query_bad_id_or_name(data):
    raises(BAD, Query.from_dict, data)


@pytest.mark.parametrize("name", ["sms.read", "calendar", "Calendar.Next", "contacts.lookup "])
def test_query_unknown_name(name):
    raises(BAD, Query.from_dict, {"id": "q", "name": name})
    raises(BAD, Query, "q", name)


@pytest.mark.parametrize("params", ["count=1", [1], 5])
def test_query_params_not_an_object(params):
    raises(BAD, Query.from_dict, {"id": "q", "name": "apps.list", "params": params})


def _query(query_name, params):
    return Query.from_dict({"id": "q", "name": query_name, "params": params})


@pytest.mark.parametrize("params", [{}, {"count": 1}, {"count": 10}, {"count": 5, "within_days": 1},
                                    {"within_days": 365}])
def test_calendar_next_ok(params):
    assert _query("calendar.next", params).params == params


@pytest.mark.parametrize("params", [
    {"count": 0}, {"count": 11}, {"count": -1}, {"count": True}, {"count": "1"}, {"count": 1.0},
    {"count": None}, {"within_days": 0}, {"within_days": -7}, {"within_days": "7"},
    {"within_days": 7.5}, {"within_days": False},
])
def test_calendar_next_bad(params):
    raises(BAD, _query, "calendar.next", params)


@pytest.mark.parametrize("params", [{"name": "Mom"}, {"name": "Mom", "limit": 1},
                                    {"name": "Mom", "limit": 100}])
def test_contacts_lookup_ok(params):
    assert _query("contacts.lookup", params).params == params


@pytest.mark.parametrize("params", [
    {}, {"name": ""}, {"name": None}, {"name": 5}, {"name": ["Mom"]},
    {"name": "Mom", "limit": 0}, {"name": "Mom", "limit": -1}, {"name": "Mom", "limit": True},
    {"name": "Mom", "limit": "5"}, {"name": "Mom", "limit": 5.0},
])
def test_contacts_lookup_bad(params):
    raises(BAD, _query, "contacts.lookup", params)


@pytest.mark.parametrize("params", [{}, {"name": "Cam"}, {"name": ""}, {"limit": 1}, {"name": "x", "limit": 50}])
def test_apps_list_ok(params):
    assert _query("apps.list", params).params == params


@pytest.mark.parametrize("params", [{"name": 5}, {"name": ["Cam"]}, {"limit": 0}, {"limit": True},
                                    {"limit": "20"}])
def test_apps_list_bad(params):
    raises(BAD, _query, "apps.list", params)


# --- responses ---------------------------------------------------------------

@pytest.mark.parametrize("data", [CALENDAR_DATA, CONTACTS_DATA, APPS_DATA, None, {}])
def test_response_success_round_trip(data):
    wire = {"id": "q", "ok": True} if data is None else {"id": "q", "ok": True, "data": data}
    response = Response.from_dict(wire)
    assert response == Response.success("q", data)
    assert response.to_dict() == wire
    assert Response.from_dict(response.to_dict()) == response


@pytest.mark.parametrize("code", list(ErrorCode))
def test_response_failure_round_trip(code):
    wire = {"id": "q", "ok": False, "error": code.value, "message": "detail"}
    response = Response.from_dict(wire)
    assert response.error is code
    assert response == Response.failure("q", code, "detail")
    assert response.to_dict() == wire


def test_response_failure_without_message():
    wire = {"id": "q", "ok": False, "error": "blocked"}
    assert Response.from_dict(wire).to_dict() == wire
    assert Response.failure("q", "blocked").to_dict() == wire


def test_response_unknown_error_code_kept():
    wire = {"id": "q", "ok": False, "error": "rate_limited", "message": "slow down"}
    response = Response.from_dict(wire)
    assert response.error == "rate_limited"
    assert response.to_dict() == wire


def test_response_unknown_fields_ignored():
    response = Response.from_dict({"id": "q", "ok": True, "data": APPS_DATA, "elapsed_ms": 12})
    assert response.to_dict() == {"id": "q", "ok": True, "data": APPS_DATA}


def test_response_ok_with_error_rejected():
    raises(BAD, Response.from_dict, {"id": "q", "ok": True, "error": "blocked"})
    raises(BAD, Response, "q", True, ErrorCode.BLOCKED)


def test_response_not_ok_without_error_rejected():
    raises(BAD, Response.from_dict, {"id": "q", "ok": False})
    raises(BAD, Response.from_dict, {"id": "q", "ok": False, "message": "no code"})
    raises(BAD, Response, "q", False)


@pytest.mark.parametrize("ok", [None, 1, 0, "true", "false"])
def test_response_bad_ok(ok):
    raises(BAD, Response.from_dict, {"id": "q", "ok": ok})


def test_response_missing_ok():
    raises(BAD, Response.from_dict, {"id": "q", "data": {}})


@pytest.mark.parametrize("error", [5, True, ["blocked"], {"code": "blocked"}])
def test_response_non_string_error(error):
    raises(BAD, Response.from_dict, {"id": "q", "ok": False, "error": error})


@pytest.mark.parametrize("data", [[], ["a"], "x", 5, True])
def test_response_non_dict_data(data):
    raises(BAD, Response.from_dict, {"id": "q", "ok": True, "data": data})


def test_response_empty_id_allowed():
    # The answer to a request that had no usable id (see WAGGLE.md "Common rules").
    assert Response.from_dict({"id": "", "ok": False, "error": "bad_request"}).id == ""


@pytest.mark.parametrize("wire", [{"ok": True}, {"id": 5, "ok": True},
                                  {"id": "q", "ok": False, "error": "blocked", "message": 5}])
def test_response_bad_id_or_message(wire):
    raises(BAD, Response.from_dict, wire)


@pytest.mark.parametrize("data", [None, [], "response"])
def test_response_not_an_object(data):
    raises(BAD, Response.from_dict, data)


def test_response_factories():
    ok = Response.success("r1", {"apps": []})
    assert ok.ok is True and ok.error is None and ok.data == {"apps": []}
    assert Response.success("r1").to_dict() == {"id": "r1", "ok": True}
    fail = Response.failure("r1", "declined", "user said no")
    assert fail.ok is False and fail.error is ErrorCode.DECLINED and fail.message == "user said no"
    assert fail.data is None
    assert Response.failure("r1", ErrorCode.TIMEOUT).error is ErrorCode.TIMEOUT
    with pytest.raises(ValueError):
        Response.failure("r1", "not_a_code")


# --- wire time ---------------------------------------------------------------

def test_to_wire_time_utc():
    assert to_wire_time(datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)) == "2026-10-04T21:00:00Z"


def test_to_wire_time_converts_non_utc():
    chicago = datetime(2026, 10, 4, 16, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert to_wire_time(chicago) == "2026-10-04T21:00:00Z"
    plus = datetime(2026, 10, 5, 1, 30, tzinfo=timezone(timedelta(hours=4, minutes=30)))
    assert to_wire_time(plus) == "2026-10-04T21:00:00Z"


def test_to_wire_time_drops_microseconds():
    assert to_wire_time(datetime(2026, 10, 4, 21, 0, 0, 999999, tzinfo=timezone.utc)) == "2026-10-04T21:00:00Z"


def test_to_wire_time_naive_raises():
    with pytest.raises(ValueError):
        to_wire_time(datetime(2026, 10, 4, 21, 0))


def test_to_wire_time_date():
    assert to_wire_time(date(2026, 10, 5)) == "2026-10-05"


def test_from_wire_time_datetime():
    value = from_wire_time("2026-10-04T21:00:00Z")
    assert value == datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


@pytest.mark.parametrize("event_field", ["dtstart", "dtend"])
def test_from_wire_time_calendar_example(event_field):
    value = CALENDAR_DATA["events"][0][event_field]
    assert to_wire_time(from_wire_time(value)) == value


def test_from_wire_time_date_only():
    value = from_wire_time("2026-10-05")
    assert value == date(2026, 10, 5)
    assert type(value) is date


@pytest.mark.parametrize("value", ["2026-10-04T21:00:00Z", "2026-10-05", "2026-02-28T00:00:00Z"])
def test_wire_time_round_trip(value):
    assert to_wire_time(from_wire_time(value)) == value


@pytest.mark.parametrize("value", ["2026-10-04T21:00:00", "2026-10-04T21:00:00+00:00",
                                   "2026-10-04T16:00:00-05:00"])
def test_from_wire_time_missing_z(value):
    raises(BAD, from_wire_time, value)


@pytest.mark.parametrize("value", ["garbage", "", "Z", "2026-13-01", "2026-02-30", "2026-10-04T25:00:00Z",
                                   "tomorrow T9", "2026-10-04Tnoon Z"])
def test_from_wire_time_garbage(value):
    raises(BAD, from_wire_time, value)


@pytest.mark.parametrize("value", [None, 1759611600, 1.5, datetime(2026, 10, 4, tzinfo=timezone.utc)])
def test_from_wire_time_not_a_string(value):
    raises(BAD, from_wire_time, value)


@pytest.mark.parametrize("value", ["2026-10-04T16:00:00-05:00Z", "2026-10-04T21:00:00+00:00Z"])
def test_from_wire_time_offset_and_z_rejected(value):
    # An offset before the Z must not be silently replaced with UTC.
    raises(BAD, from_wire_time, value)


@pytest.mark.parametrize("value", ["20261005", "2026-W41-1", "2026-278", "26-10-05", "2026-10-5"])
def test_from_wire_time_date_only_must_be_yyyy_mm_dd(value):
    raises(BAD, from_wire_time, value)


# --- ids and URIs ------------------------------------------------------------

def test_new_request_id_is_uuid4():
    value = new_request_id()
    assert isinstance(value, str)
    parsed = uuid.UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value
    assert len({new_request_id() for _ in range(100)}) == 100


@pytest.mark.parametrize("uri, scheme", [
    ("tel:+15551234567", "tel"), ("TEL:+15551234567", "tel"), ("HtTpS://example.com/", "https"),
    ("smsto:", "smsto"), ("android-app://com.example", "android-app"), ("geo:0,0?q=x", "geo"),
    ("web+app:x", "web+app"), ("example.com/x", None), ("//example.com/x", None), ("/sdcard/x", None),
    ("", None),
])
def test_uri_scheme(uri, scheme):
    assert uri_scheme(uri) == scheme
