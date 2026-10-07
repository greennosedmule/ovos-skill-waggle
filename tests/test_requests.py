from __future__ import annotations

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle import requests as r
from ovos_skill_waggle.requests import RequestResponse, WaggleRequest
from waggle.messages import ErrorCode, WaggleError


def assert_bad_request(fn, *args, **kwargs):
    with pytest.raises(WaggleError) as e:
        fn(*args, **kwargs)
    assert e.value.code is ErrorCode.BAD_REQUEST


def test_names():
    assert (r.REQUEST, r.REQUEST_RESPONSE) == ("waggle:request", "waggle:request.response")
    assert r.REQUEST_NAMES == ("alarm.set", "timer.set", "alarms.show", "calendar.next",
                               "app.open", "contact.call", "message.compose")
    assert r.UNREACHABLE == "unreachable"


# --- WaggleRequest -----------------------------------------------------------

VALID = [
    ("alarm.set", {"hour": 6, "minute": 30}),
    ("alarm.set", {"hour": 0, "minute": 0, "label": "Work"}),
    ("alarm.set", {"hour": 23, "minute": 59, "label": ""}),
    ("timer.set", {"seconds": 1}),
    ("timer.set", {"seconds": 86400, "label": "Oven"}),
    ("alarms.show", {}),
    ("calendar.next", {}),
    ("calendar.next", {"count": 1}),
    ("calendar.next", {"count": 10}),
    ("app.open", {"name": "Camera"}),
    ("contact.call", {"name": "Mom"}),
    ("message.compose", {"name": "Mom", "body": "Running late"}),
]


@pytest.mark.parametrize("request_,params", VALID)
def test_valid_request_round_trips(request_, params):
    d = {"request": request_, "params": params, "speak": False}
    req = WaggleRequest.from_dict(d)
    assert (req.request, req.params, req.speak) == (request_, params, False)
    assert req.to_dict() == d
    assert WaggleRequest.from_dict(req.to_dict()) == req


INVALID = [
    ("alarm.set", {}),
    ("alarm.set", {"hour": 6}),
    ("alarm.set", {"minute": 30}),
    ("alarm.set", {"hour": 24, "minute": 0}),
    ("alarm.set", {"hour": -1, "minute": 0}),
    ("alarm.set", {"hour": 6, "minute": 60}),
    ("alarm.set", {"hour": 6, "minute": -1}),
    ("alarm.set", {"hour": "6", "minute": 30}),
    ("alarm.set", {"hour": 6.0, "minute": 30}),
    ("alarm.set", {"hour": True, "minute": 30}),
    ("alarm.set", {"hour": 6, "minute": 30, "label": 5}),
    ("timer.set", {}),
    ("timer.set", {"seconds": 0}),
    ("timer.set", {"seconds": 86401}),
    ("timer.set", {"seconds": 60.5}),
    ("timer.set", {"seconds": "600"}),
    ("timer.set", {"seconds": 600, "label": ["x"]}),
    ("calendar.next", {"count": 0}),
    ("calendar.next", {"count": 11}),
    ("calendar.next", {"count": "2"}),
    ("app.open", {}),
    ("app.open", {"name": ""}),
    ("app.open", {"name": "   "}),
    ("app.open", {"name": 3}),
    ("contact.call", {}),
    ("contact.call", {"name": ""}),
    ("message.compose", {"name": "Mom"}),
    ("message.compose", {"body": "hi"}),
    ("message.compose", {"name": "Mom", "body": ""}),
    ("message.compose", {"name": "", "body": "hi"}),
]


@pytest.mark.parametrize("request_,params", INVALID)
def test_invalid_params(request_, params):
    assert_bad_request(WaggleRequest.from_dict, {"request": request_, "params": params})
    assert_bad_request(WaggleRequest, request_, params)


def test_unknown_params_ignored():
    req = WaggleRequest.from_dict({"request": "timer.set",
                                   "params": {"seconds": 60, "repeat": True}, "extra": 1})
    assert req.params == {"seconds": 60}
    assert WaggleRequest("alarms.show", {"anything": 1}).params == {}


def test_null_optional_param_is_absent():
    assert WaggleRequest("alarm.set", {"hour": 6, "minute": 0, "label": None}).params == {
        "hour": 6, "minute": 0}


def test_params_are_copied():
    params = {"seconds": 60}
    req = WaggleRequest("timer.set", params)
    params["seconds"] = 0
    assert req.params == {"seconds": 60}


def test_defaults():
    req = WaggleRequest.from_dict({"request": "alarms.show"})
    assert req.params == {} and req.speak is True
    assert req.to_dict() == {"request": "alarms.show", "params": {}, "speak": True}
    assert WaggleRequest("alarms.show").speak is True


@pytest.mark.parametrize("d", [
    None, [], {}, {"request": "alarm.cancel"}, {"request": 5}, {"request": "Alarm.Set"},
    {"request": "alarms.show", "params": []}, {"request": "alarms.show", "params": "x"},
    {"request": "alarms.show", "speak": "yes"}, {"request": "alarms.show", "speak": 1},
    {"request": "alarms.show", "speak": None},
])
def test_malformed_request(d):
    assert_bad_request(WaggleRequest.from_dict, d)


# --- RequestResponse ---------------------------------------------------------

def test_success_round_trip():
    resp = RequestResponse.success("10-minute timer started.")
    assert resp.to_dict() == {"ok": True, "summary": "10-minute timer started."}
    assert RequestResponse.from_dict(resp.to_dict()) == resp


@pytest.mark.parametrize("error", [c.value for c in ErrorCode] + ["unreachable"])
def test_failure_round_trip(error):
    resp = RequestResponse.failure(error, "It didn't work.")
    assert resp.to_dict() == {"ok": False, "summary": "It didn't work.", "error": error}
    assert RequestResponse.from_dict(resp.to_dict()) == resp


def test_error_code_enum_stored_as_string():
    resp = RequestResponse(False, "You declined on your phone.", ErrorCode.DECLINED)
    assert resp.error == "declined" and type(resp.error) is str
    assert resp.to_dict()["error"] == "declined"


def test_unknown_fields_ignored():
    assert RequestResponse.from_dict({"ok": True, "summary": "Done.", "id": "x"}) == \
        RequestResponse(True, "Done.")


@pytest.mark.parametrize("d", [
    {"ok": True, "summary": "Done.", "error": "blocked"},   # error with ok
    {"ok": False, "summary": "Failed."},                     # ok false without error
    {"ok": False, "summary": "Failed.", "error": "nope"},    # not a known code
    {"ok": False, "summary": "Failed.", "error": 3},
    {"ok": "true", "summary": "Done."},
    {"summary": "Done."},
    {"ok": True},
    {"ok": True, "summary": ""},
    {"ok": True, "summary": "  "},
    {"ok": True, "summary": 5},
    None,
])
def test_malformed_response(d):
    assert_bad_request(RequestResponse.from_dict, d)


# --- bus messages ------------------------------------------------------------

def test_request_message():
    ctx = {"source": "hive", "destination": "skills", "session": {"session_id": "s"}}
    req = WaggleRequest("timer.set", {"seconds": 600})
    msg = r.request_message(req, ctx)
    assert msg.msg_type == "waggle:request"
    assert msg.data == {"request": "timer.set", "params": {"seconds": 600}, "speak": True}
    assert msg.context == ctx and msg.context is not ctx
    assert WaggleRequest.from_dict(Message.deserialize(msg.serialize()).data) == req


def test_request_message_without_context():
    msg = r.request_message(WaggleRequest("alarms.show"))
    assert msg.context == {}


def test_response_message_replies_to_origin():
    origin = r.request_message(WaggleRequest("alarms.show"),
                               {"source": "skills", "destination": "hive:peer"})
    msg = r.response_message(origin, RequestResponse.failure(r.UNREACHABLE,
                                                             "I couldn't reach your phone."))
    assert msg.msg_type == "waggle:request.response"
    assert msg.data == {"ok": False, "summary": "I couldn't reach your phone.",
                        "error": "unreachable"}
    assert msg.context["source"] == "hive:peer" and msg.context["destination"] == "skills"
