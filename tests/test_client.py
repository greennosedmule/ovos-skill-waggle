"""WaggleClient: id matching, timeouts, malformed responses, concurrency, cleanup."""
from __future__ import annotations

import threading

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from waggle.client import WaggleClient
from waggle.messages import (
    INTENT, INTENT_RESPONSE, QUERY, QUERY_RESPONSE, ErrorCode, Intent, Query, Response,
)

ORIGIN = Message("recognizer_loop:utterance", {}, {"source": "phone-peer", "destination": "skills"})


def intent(id="req-1"):
    return Intent(id=id, action="android.intent.action.SET_TIMER")


class Responder:
    """Answers requests with whatever ``reply`` returns (a list of data dicts)."""

    def __init__(self, bus, msg_type, reply):
        self.bus, self.reply, self.requests = bus, reply, []
        self.response_type = msg_type + ".response"
        bus.on(msg_type, self.handle)

    def handle(self, message):
        self.requests.append(message)
        for data in self.reply(message):
            self.bus.emit(Message(self.response_type, data, {"source": "phone-peer"}))


def listeners(bus, msg_type):
    return bus.ee.listeners(msg_type)


@pytest.fixture
def bus():
    return FakeBus()


def test_intent_round_trip_routes_as_reply(bus):
    responder = Responder(bus, INTENT, lambda m: [{"id": m.data["id"], "ok": True}])
    response = WaggleClient(bus).send_intent(ORIGIN, intent(), 0.5)
    assert response == Response.success("req-1")
    sent = responder.requests[0]
    assert sent.data == intent().to_dict()
    assert sent.context["destination"] == "phone-peer"  # Message.reply swapped it
    assert listeners(bus, INTENT_RESPONSE) == []


def test_query_round_trip(bus):
    Responder(bus, QUERY, lambda m: [{"id": m.data["id"], "ok": True, "data": {"apps": []}}])
    response = WaggleClient(bus).send_query(ORIGIN, Query("q-1", "apps.list"), 0.5)
    assert response.ok and response.data == {"apps": []}
    assert listeners(bus, QUERY_RESPONSE) == []


def test_failure_response_parsed(bus):
    Responder(bus, INTENT, lambda m: [{"id": m.data["id"], "ok": False, "error": "declined"}])
    response = WaggleClient(bus).send_intent(ORIGIN, intent(), 0.5)
    assert response.error is ErrorCode.DECLINED


def test_timeout_returns_none_and_cleans_up(bus):
    assert WaggleClient(bus).send_intent(ORIGIN, intent(), 0.05) is None
    assert listeners(bus, INTENT_RESPONSE) == []


def test_ignores_other_ids_and_malformed(bus):
    def reply(m):
        rid = m.data["id"]
        return [
            {"id": "someone-else", "ok": True},
            {"id": rid, "ok": "yes"},                    # malformed: ok not a bool
            {"id": rid, "ok": True, "error": "blocked"},  # malformed: error with ok
            {"id": rid, "ok": False, "error": "blocked"},
            {"id": rid, "ok": True},                      # a second answer is ignored
        ]
    Responder(bus, INTENT, reply)
    response = WaggleClient(bus).send_intent(ORIGIN, intent(), 0.5)
    assert response == Response.failure("req-1", ErrorCode.BLOCKED)


def test_only_malformed_times_out(bus):
    Responder(bus, INTENT, lambda m: [{"id": m.data["id"], "ok": None}])
    assert WaggleClient(bus).send_intent(ORIGIN, intent(), 0.05) is None


def test_unknown_error_code_kept(bus):
    Responder(bus, INTENT, lambda m: [{"id": m.data["id"], "ok": False, "error": "new_code"}])
    assert WaggleClient(bus).send_intent(ORIGIN, intent(), 0.5).error == "new_code"


def test_handler_removed_when_emit_raises():
    class BrokenBus(FakeBus):
        def emit(self, message):
            raise RuntimeError("socket closed")
    bus = BrokenBus()
    with pytest.raises(RuntimeError):
        WaggleClient(bus).send_intent(ORIGIN, intent(), 0.05)
    assert listeners(bus, INTENT_RESPONSE) == []


def test_origin_none_sends_plain_message(bus):
    responder = Responder(bus, INTENT, lambda m: [{"id": m.data["id"], "ok": True}])
    assert WaggleClient(bus).send_intent(None, intent(), 0.5).ok
    assert "destination" not in responder.requests[0].context


def test_concurrent_requests_answered_in_reverse_order(bus):
    """Two requests in flight on two threads; answers arrive in reverse order."""
    pending, both_in = [], threading.Event()

    def on_intent(message):
        pending.append(message.data["id"])
        if len(pending) == 2:
            both_in.set()
    bus.on(INTENT, on_intent)

    client = WaggleClient(bus)
    results = {}

    def send(rid):
        results[rid] = client.send_intent(ORIGIN, intent(rid), 2)
    threads = [threading.Thread(target=send, args=(rid,)) for rid in ("a", "b")]
    for t in threads:
        t.start()
    assert both_in.wait(1)
    for rid in reversed(pending):
        bus.emit(Message(INTENT_RESPONSE, {"id": rid, "ok": False, "error": "declined",
                                           "message": rid}))
    for t in threads:
        t.join(2)
    assert results["a"].message == "a" and results["b"].message == "b"
    assert listeners(bus, INTENT_RESPONSE) == []


def test_send_raw_adds_id_only_if_missing(bus):
    responder = Responder(bus, INTENT, lambda m: [{"id": m.data.get("id"), "ok": True}])
    client = WaggleClient(bus)
    assert client.send_raw(ORIGIN, INTENT, {"action": "x"}, 0.5).ok
    assert responder.requests[-1].data["id"]
    assert client.send_raw(ORIGIN, INTENT, {"id": "mine", "action": "x"}, 0.5).id == "mine"


def test_send_raw_with_unusable_id_matches_empty_id(bus):
    Responder(bus, INTENT, lambda m: [{"id": "", "ok": False, "error": "bad_request"}])
    client = WaggleClient(bus)
    for bad_id in (42, None, ""):
        response = client.send_raw(ORIGIN, INTENT, {"id": bad_id, "action": "x"}, 0.5)
        assert response.id == "" and response.error is ErrorCode.BAD_REQUEST


def test_send_raw_rejects_non_request_type(bus):
    with pytest.raises(ValueError):
        WaggleClient(bus).send_raw(ORIGIN, INTENT_RESPONSE, {}, 0.1)
