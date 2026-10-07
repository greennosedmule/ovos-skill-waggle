"""The request handlers with the fake phone: every outcome, timeouts, speak and routing."""
from __future__ import annotations

import time

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.handlers import phone_origin, spoken_clock, spoken_duration
from ovos_skill_waggle.requests import REQUEST, REQUEST_RESPONSE, RequestResponse
from waggle.fake_phone import AskAnswer
from waggle.intents import ACTION_SET_ALARM, ACTION_SET_TIMER, ACTION_SHOW_ALARMS
from waggle.messages import (
    CAPABILITIES, INTENT, INTENT_RESPONSE, ErrorCode, Intent, Response,
)

from conftest import OTHER_PEER, PEER, capabilities, match_message, utterance

HANDLED = "ovos.utterance.handled"
TIMER = ("timer.set", {"seconds": 600})


def request(name, params=None, speak=True, peer=PEER, **extra):
    """A ``waggle:request`` carrying the utterance's context, as SPEC asks."""
    context = {"source": peer, "peer": peer, "destination": "skills"} if peer else {}
    return Message(REQUEST, {"request": name, "params": params or {}, "speak": speak, **extra},
                   context)


def run(bus, recorder, message):
    """Emit a request; the response it got, parsed."""
    recorder.clear()
    bus.emit(message)
    responses = recorder.of(REQUEST_RESPONSE)
    assert len(responses) == 1
    return RequestResponse.from_dict(responses[0].data)


def spoken(recorder):
    return [m.data["utterance"] for m in recorder.of("speak")]


def ask_rules(action=ACTION_SET_TIMER, mode="ask"):
    return capabilities(rules=[{"action": action, "mode": mode}], ask_timeout_s=0.3)


# --- success -----------------------------------------------------------------

@pytest.mark.parametrize("name,params,action,said", [
    ("timer.set", {"seconds": 600}, ACTION_SET_TIMER, "Timer set for 10 minutes."),
    ("timer.set", {"seconds": 5400}, ACTION_SET_TIMER, "Timer set for 1 hour and 30 minutes."),
    ("timer.set", {"seconds": 480, "label": "pasta"}, ACTION_SET_TIMER,
     "pasta timer set for 8 minutes."),
    ("alarm.set", {"hour": 6, "minute": 30}, ACTION_SET_ALARM, "Alarm set for 6:30 AM."),
    ("alarm.set", {"hour": 18, "minute": 5}, ACTION_SET_ALARM, "Alarm set for 6:05 PM."),
    ("alarm.set", {"hour": 7, "minute": 0, "label": "work"}, ACTION_SET_ALARM,
     "work alarm set for 7:00 AM."),
    ("alarms.show", {}, ACTION_SHOW_ALARMS, "Here are your alarms."),
])
def test_ok(bus, recorder, phone, name, params, action, said):
    response = run(bus, recorder, request(name, params))
    assert response == RequestResponse.success(said)
    assert spoken(recorder) == [said]
    sent = recorder.of(INTENT)
    assert len(sent) == 1 and sent[0].data["action"] == action
    assert phone.responses[0].ok


def test_intent_extras(bus, recorder, phone):
    run(bus, recorder, request("alarm.set", {"hour": 6, "minute": 30, "label": "Work"}))
    intent = Intent.from_dict(recorder.of(INTENT)[0].data)
    assert intent.extras["android.intent.extra.alarm.HOUR"].value == 6
    assert intent.extras["android.intent.extra.alarm.MINUTES"].value == 30
    assert intent.extras["android.intent.extra.alarm.MESSAGE"].value == "Work"
    assert intent.extras["android.intent.extra.alarm.SKIP_UI"].value is True
    assert intent.description == 'Set an alarm for 6:30 AM labeled "Work"'


def test_package_override(make_pipeline, make_phone, bus, recorder):
    make_pipeline({"package_timer_set": "com.example.clock"})
    make_phone()
    run(bus, recorder, request(*TIMER))
    assert recorder.of(INTENT)[0].data["package"] == "com.example.clock"


# --- routing -----------------------------------------------------------------

def test_messages_to_the_phone_are_addressed_to_its_peer(bus, recorder, phone):
    message = request(*TIMER)
    message.context["session"] = {"session_id": "phone-session"}
    run(bus, recorder, message)
    for msg_type in (INTENT, "speak"):
        sent = recorder.of(msg_type)[0]
        assert sent.context["destination"] == PEER, msg_type
        assert sent.context["session"]["session_id"] == "phone-session"
    # The response goes back to whoever made the request, as a reply to it.
    assert recorder.of(REQUEST_RESPONSE)[0].context["destination"] == PEER


def test_match_message_routes_to_the_phone(bus, recorder, pipeline, phone):
    # ovos-core's match message is already a reply (destination = the peer); a
    # second reply would go back to "skills", so the handler re-addresses it.
    utt = utterance("set a timer for 10 minutes")
    m = pipeline.match_high(utt.data["utterances"], "en-US", utt)
    recorder.clear()
    bus.emit(match_message(utt, m))
    assert recorder.of(INTENT)[0].context["destination"] == PEER
    assert recorder.of("speak")[0].context["destination"] == PEER
    assert phone.responses and phone.responses[0].ok


def test_phone_origin():
    original = Message("x", {}, {"source": PEER, "destination": "skills", "session": {"a": 1}})
    as_reply = original.reply("y")
    for message in (original, as_reply):
        origin = phone_origin(message, PEER)
        assert origin.reply("z").context["destination"] == PEER
        assert origin.reply("z").context["source"] == "skills"
        assert origin.context["session"] == {"a": 1}
    # A message from elsewhere (S4: the persona asking for another peer).
    elsewhere = Message("x", {}, {"source": "persona", "destination": "skills"})
    assert phone_origin(elsewhere, PEER).reply("z").context["destination"] == PEER
    assert phone_origin(Message("x"), PEER).reply("z").context == \
        {"source": "skills", "destination": PEER}


def test_two_phones_each_get_their_own(bus, recorder, pipeline, make_phone):
    mine = make_phone()
    theirs = make_phone(peer=OTHER_PEER)
    run(bus, recorder, request(*TIMER, peer=OTHER_PEER))
    assert len(theirs.log) == 1 and not mine.log


# --- failures from the phone -------------------------------------------------

def test_declined(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=ask_rules(), ask_answer=AskAnswer.DECLINE)
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("declined", "Okay, cancelled.")
    assert spoken(recorder) == ["Confirm on your phone.", "Okay, cancelled."]


def test_ask_accepted(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=ask_rules())
    response = run(bus, recorder, request(*TIMER))
    assert response.ok
    assert spoken(recorder) == ["Confirm on your phone.", "Timer set for 10 minutes."]


def test_ask_timeout_on_the_phone(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=ask_rules(), ask_answer=AskAnswer.NONE, timeout_delay_s=0.1)
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("timeout", "You didn't confirm on your phone.")


@pytest.mark.parametrize("code,said", [
    (ErrorCode.NO_HANDLER, "There's no app on your phone that can do that."),
    (ErrorCode.LAUNCH_FAILED, "Your phone couldn't open the app. Try again with your phone unlocked."),
    (ErrorCode.PERMISSION_DENIED, "Your phone doesn't have permission for that."),
    (ErrorCode.BAD_REQUEST, "Something went wrong on your phone."),
    (ErrorCode.UNSUPPORTED_VERSION,
     "Your phone and the hub are on different versions, so I can't do that."),
])
def test_phone_errors(bus, recorder, pipeline, make_phone, code, said):
    make_phone(launch=code)
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure(code.value, said)
    assert spoken(recorder) == [said]


def test_blocked_by_the_phone(bus, recorder, pipeline, phone):
    # The announcement allowed it, but the phone has since changed its mind.
    phone.capabilities = capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "block"}])
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("blocked", "Your phone doesn't allow that.")
    assert len(recorder.of(INTENT)) == 1


def test_unknown_error_code_from_a_newer_phone(bus, recorder, pipeline):
    def answer(message):
        bus.emit(Message(INTENT_RESPONSE, {"id": message.data["id"], "ok": False,
                                           "error": "battery_low"}, {"source": PEER}))

    bus.emit(Message(CAPABILITIES, capabilities().to_dict(), {"source": PEER, "peer": PEER}))
    bus.on(INTENT, answer)
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("bad_request", "Something went wrong on your phone.")


# --- failures on the hub -----------------------------------------------------

def test_blocked_by_rule_not_sent(bus, recorder, pipeline, make_phone):
    phone = make_phone(capabilities=capabilities(
        rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("blocked", "Your phone doesn't allow that.")
    assert recorder.of(INTENT) == [] and phone.log == []


def test_unmatched_block_not_sent(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(unmatched="block", rules=[]))
    assert run(bus, recorder, request("alarms.show")).error == "blocked"
    assert recorder.of(INTENT) == []


def test_no_capabilities(bus, recorder, pipeline):
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("unreachable", "Your phone isn't connected.")
    assert recorder.of(INTENT) == []


def test_unsupported_version(bus, recorder, pipeline):
    bus.emit(Message(CAPABILITIES, {**capabilities().to_dict(), "version": 2},
                     {"source": PEER, "peer": PEER}))
    response = run(bus, recorder, request(*TIMER))
    assert response.error == "unsupported_version"
    assert recorder.of(INTENT) == []


def test_disabled(make_pipeline, make_phone, bus, recorder):
    make_pipeline({"enable_timer_set": False})
    make_phone()
    response = run(bus, recorder, request(*TIMER))
    assert response == RequestResponse.failure("blocked", "That's turned off on the hub.")
    assert recorder.of(INTENT) == []


@pytest.mark.parametrize("name,params", [("calendar.next", {}), ("app.open", {"name": "Camera"})])
def test_not_yet_supported(bus, recorder, phone, name, params):
    response = run(bus, recorder, request(name, params))
    assert response == RequestResponse.failure("bad_request", "I can't do that on your phone yet.")


@pytest.mark.parametrize("data", [
    {"request": "timer.set", "params": {"seconds": 0}},
    {"request": "timer.launch", "params": {}},
    {"request": "alarm.set", "params": {"hour": 6}},
    {"params": {}},
])
def test_bad_request(bus, recorder, phone, data):
    message = Message(REQUEST, data, {"source": PEER, "peer": PEER, "destination": "skills"})
    response = run(bus, recorder, message)
    assert response == RequestResponse.failure("bad_request", "Sorry, something went wrong.")
    assert recorder.of(INTENT) == []


def test_bad_request_not_spoken_when_speak_false(bus, recorder, phone):
    run(bus, recorder, request("timer.set", {"seconds": -1}, speak=False))
    assert spoken(recorder) == []


def test_internal_error_still_answers(bus, recorder, pipeline, phone, monkeypatch):
    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(pipeline.handlers.client, "send_intent", boom)
    assert run(bus, recorder, request(*TIMER)).error == "bad_request"


# --- timeouts ----------------------------------------------------------------

def test_unreachable(bus, recorder, pipeline, make_phone):
    make_phone(silent=True)
    start = time.monotonic()
    response = run(bus, recorder, request(*TIMER))
    waited = time.monotonic() - start
    assert response == RequestResponse.failure("unreachable", "I couldn't reach your phone.")
    assert 0.3 <= waited < 1.0  # response_timeout_s


def test_run_rule_waits_response_timeout(bus, recorder, pipeline, make_phone):
    make_phone(response_delay_s=0.6)  # longer than response_timeout_s (0.3)
    assert run(bus, recorder, request(*TIMER)).error == "unreachable"


def test_ask_rule_waits_ask_timeout_plus_margin(bus, recorder, pipeline, make_phone):
    # The same 0.6 s answer is in time for an ask rule: ask_timeout_s 0.5 + ask_margin_s 0.2.
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "ask"}],
                                         ask_timeout_s=0.5), response_delay_s=0.6)
    start = time.monotonic()
    assert run(bus, recorder, request(*TIMER)).ok
    assert time.monotonic() - start < 0.7


def test_ask_rule_still_times_out(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "ask"}],
                                         ask_timeout_s=0.2), silent=True)
    start = time.monotonic()
    assert run(bus, recorder, request(*TIMER)).error == "unreachable"
    assert 0.4 <= time.monotonic() - start < 1.0


def test_unmatched_ask_uses_ask_timeout(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(unmatched="ask", rules=[], ask_timeout_s=0.5),
               response_delay_s=0.6)
    assert run(bus, recorder, request(*TIMER)).ok


# --- speak and ovos.utterance.handled ----------------------------------------

def test_speak_false(bus, recorder, phone):
    response = run(bus, recorder, request(*TIMER, speak=False))
    assert response == RequestResponse.success("Timer set for 10 minutes.")  # summary for the caller
    assert spoken(recorder) == []
    assert recorder.of(HANDLED) == []


@pytest.mark.parametrize("setup", ["silent", "blocked", "declined"])
def test_speak_false_failures_are_silent(bus, recorder, pipeline, make_phone, setup):
    if setup == "silent":
        make_phone(silent=True)
    elif setup == "blocked":
        make_phone(capabilities=capabilities(rules=[{"action": ACTION_SET_TIMER, "mode": "block"}]))
    else:
        make_phone(capabilities=ask_rules(), ask_answer=AskAnswer.DECLINE)
    response = run(bus, recorder, request(*TIMER, speak=False))
    assert not response.ok and response.summary
    assert spoken(recorder) == [] and recorder.of(HANDLED) == []


def test_plain_request_never_emits_handled(bus, recorder, phone):
    # Whoever made the request owns the utterance and says when it's handled.
    run(bus, recorder, request(*TIMER, speak=True))
    assert recorder.of(HANDLED) == []


@pytest.mark.parametrize("phone_setup", [{}, {"silent": True}, {"launch": ErrorCode.NO_HANDLER}])
def test_pipeline_request_emits_handled_after_speaking(bus, recorder, pipeline, make_phone,
                                                        phone_setup):
    make_phone(**phone_setup)
    utt = utterance("set a timer for 10 minutes")
    m = pipeline.match_high(utt.data["utterances"], "en-US", utt)
    recorder.clear()
    bus.emit(match_message(utt, m))
    types = recorder.types()
    assert types.count(HANDLED) == 1
    assert types.index("speak") < types.index(REQUEST_RESPONSE) < types.index(HANDLED)
    handled = recorder.of(HANDLED)[0]
    assert handled.context["destination"] == PEER  # reaches the phone, ending its turn


def test_handled_even_if_the_handler_breaks(bus, recorder, pipeline, phone, monkeypatch):
    def boom(message):
        raise RuntimeError("bug")

    monkeypatch.setattr(pipeline.handlers, "handle", boom)
    utt = utterance("set a timer for 10 minutes")
    m = pipeline.match_high(utt.data["utterances"], "en-US", utt)
    recorder.clear()
    bus.emit(match_message(utt, m))
    assert recorder.of(HANDLED)


# --- spoken forms ------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,text", [
    (0, 0, "12:00 AM"), (0, 5, "12:05 AM"), (6, 30, "6:30 AM"), (12, 0, "12:00 PM"),
    (18, 30, "6:30 PM"), (23, 59, "11:59 PM"),
])
def test_spoken_clock(hour, minute, text):
    assert spoken_clock(hour, minute) == text


@pytest.mark.parametrize("seconds,text", [
    (1, "1 second"), (60, "1 minute"), (600, "10 minutes"), (3600, "1 hour"),
    (5400, "1 hour and 30 minutes"), (8110, "2 hours, 15 minutes and 10 seconds"),
    (86400, "24 hours"),
])
def test_spoken_duration(seconds, text):
    assert spoken_duration(seconds) == text


def test_response_parses_as_waggle_response(bus, recorder, phone):
    run(bus, recorder, request(*TIMER))
    Response.from_dict(recorder.of(INTENT_RESPONSE)[0].data)
