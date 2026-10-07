"""FakePhone: every outcome a hub handler can see, on a FakeBus."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from waggle.client import WaggleClient
from waggle.fake_phone import DEFAULT_CAPABILITIES, AskAnswer, FakePhone
from waggle.messages import (
    CAPABILITIES, INTENT, INTENT_RESPONSE, QUERY, Capabilities, ErrorCode,
    Intent, Mode, Query, new_request_id,
)

A = "android.intent.action."
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def origin(peer="fake-phone"):
    """A message from the phone; replies to it are addressed to the phone."""
    return Message("recognizer_loop:utterance", {}, {"source": peer, "destination": "skills"})


def make_intent(action, **kw):
    return Intent(id=new_request_id(), action=A + action, **kw)


@pytest.fixture
def bus():
    return FakeBus()


@pytest.fixture
def client(bus):
    return WaggleClient(bus)


def send(client, intent, peer="fake-phone", timeout_s=0.5):
    return client.send_intent(origin(peer), intent, timeout_s)


def query(client, query_name, peer="fake-phone", timeout_s=0.5, **params):
    return client.send_query(origin(peer), Query(new_request_id(), query_name, params), timeout_s)


# --- intents -----------------------------------------------------------------

def test_run_rule_launches(bus, client):
    phone = FakePhone(bus)
    intent = make_intent("SET_ALARM")
    response = send(client, intent)
    assert response.ok and response.id == intent.id and response.error is None
    entry = phone.log[-1]
    assert entry.decision.mode is Mode.RUN and entry.response == response
    assert entry.request.data["id"] == intent.id


@pytest.mark.parametrize("answer, ok, error", [
    (AskAnswer.ACCEPT, True, None),
    (AskAnswer.DECLINE, False, ErrorCode.DECLINED),
    (AskAnswer.NONE, False, ErrorCode.TIMEOUT),
])
def test_ask_rule(bus, client, answer, ok, error):
    phone = FakePhone(bus, ask_answer=answer)
    response = send(client, make_intent("DIAL", data="tel:+15551234567"))
    assert (response.ok, response.error) == (ok, error)
    assert phone.log[-1].decision.mode is Mode.ASK


def test_ask_timeout_is_delayed(bus, client):
    FakePhone(bus, ask_answer=AskAnswer.NONE, timeout_delay_s=0.1)
    assert send(client, make_intent("DIAL", data="tel:1"), timeout_s=0.02) is None
    response = send(client, make_intent("DIAL", data="tel:1"), timeout_s=1)
    assert response.error is ErrorCode.TIMEOUT


def test_sms_scheme_asks_by_default(bus, client):
    phone = FakePhone(bus)
    assert send(client, make_intent("SENDTO", data="sms:+15551234567")).ok
    assert phone.log[-1].decision.mode is Mode.ASK


def test_block_rule(bus, client):
    caps = Capabilities.from_dict({**DEFAULT_CAPABILITIES.to_dict(), "rules": [
        {"action": A + "DIAL", "mode": "run"},
        {"action": A + "DIAL", "scheme": "tel", "mode": "block"}]})
    phone = FakePhone(bus, capabilities=caps)
    response = send(client, make_intent("DIAL", data="TEL:1"))
    assert response.error is ErrorCode.BLOCKED
    assert phone.log[-1].decision.rule.mode is Mode.BLOCK


def test_unmatched_blocks_by_default(bus, client):
    phone = FakePhone(bus)
    assert send(client, make_intent("VIEW", data="https://example.com")).error is ErrorCode.BLOCKED
    assert phone.log[-1].decision.rule is None


@pytest.mark.parametrize("answer, ok", [(AskAnswer.ACCEPT, True), (AskAnswer.DECLINE, False)])
def test_unmatched_ask(bus, client, answer, ok):
    caps = Capabilities.from_dict({**DEFAULT_CAPABILITIES.to_dict(), "unmatched": "ask"})
    phone = FakePhone(bus, capabilities=caps, ask_answer=answer)
    assert send(client, make_intent("VIEW", data="https://example.com")).ok is ok
    assert phone.log[-1].decision.mode is Mode.ASK and phone.log[-1].decision.rule is None


@pytest.mark.parametrize("outcome", [ErrorCode.NO_HANDLER, ErrorCode.LAUNCH_FAILED])
def test_launch_outcome_global(bus, client, outcome):
    FakePhone(bus, launch=outcome)
    assert send(client, make_intent("SET_TIMER")).error is outcome
    assert send(client, make_intent("DIAL", data="tel:1")).error is outcome  # after an accepted ask


def test_launch_outcome_per_action(bus, client):
    FakePhone(bus, launch=ErrorCode.LAUNCH_FAILED,
              launch_by_action={A + "SET_ALARM": ErrorCode.NO_HANDLER, A + "SET_TIMER": None})
    assert send(client, make_intent("SET_ALARM")).error is ErrorCode.NO_HANDLER
    assert send(client, make_intent("SET_TIMER")).ok
    assert send(client, make_intent("SHOW_ALARMS")).error is ErrorCode.LAUNCH_FAILED


def test_blocked_beats_launch_outcome(bus, client):
    FakePhone(bus, launch=ErrorCode.NO_HANDLER)
    assert send(client, make_intent("VIEW")).error is ErrorCode.BLOCKED


def test_silent_never_answers_but_logs(bus, client):
    phone = FakePhone(bus, silent=True)
    assert send(client, make_intent("SET_ALARM"), timeout_s=0.05) is None
    assert query(client, "apps.list", timeout_s=0.05) is None
    assert [e.response for e in phone.log] == [None, None]
    assert phone.responses == []


def test_response_delay(bus, client):
    FakePhone(bus, response_delay_s=0.1)
    assert send(client, make_intent("SET_ALARM"), timeout_s=0.02) is None
    assert send(client, make_intent("SET_ALARM"), timeout_s=1).ok


def test_settings_can_change_between_requests(bus, client):
    phone = FakePhone(bus)
    assert send(client, make_intent("DIAL", data="tel:1")).ok
    phone.ask_answer = AskAnswer.DECLINE
    assert send(client, make_intent("DIAL", data="tel:1")).error is ErrorCode.DECLINED


# --- bad requests ------------------------------------------------------------

@pytest.mark.parametrize("data", [
    {"action": A + "VIEW", "data": "content://contacts/people/1"},
    {"action": A + "VIEW", "data": "File:///sdcard/x"},
    {"action": A + "VIEW", "extras": {"u": {"type": "uri", "value": "intent:#Intent;end"}}},
    {"action": A + "SET_TIMER", "extras": {"x": {"type": "parcelable", "value": 1}}},
    {"action": A + "SET_TIMER", "extras": {"x": {"type": "int", "value": "ten"}}},
    {"action": 7},
    {},
    {"action": A + "MAIN", "categories": "LAUNCHER"},
])
def test_bad_intents(bus, client, data):
    phone = FakePhone(bus)
    response = client.send_raw(origin(), INTENT, {"id": "bad-1", **data}, 0.5)
    assert (response.id, response.error) == ("bad-1", ErrorCode.BAD_REQUEST)
    assert response.message
    assert phone.log[-1].decision is None


@pytest.mark.parametrize("data", [
    {"name": "sms.read"},
    {"name": "contacts.lookup", "params": {}},
    {"name": "calendar.next", "params": {"count": 11}},
    {"name": "apps.list", "params": ["x"]},
    {"params": {}},
])
def test_bad_queries(bus, client, data):
    FakePhone(bus)
    response = client.send_raw(origin(), QUERY, {"id": "bad-q", **data}, 0.5)
    assert (response.id, response.error) == ("bad-q", ErrorCode.BAD_REQUEST)


@pytest.mark.parametrize("msg_type", [INTENT, QUERY])
@pytest.mark.parametrize("bad_id", [None, 42, ""])
def test_unusable_id_answered_once_with_empty_id(bus, msg_type, bad_id):
    phone = FakePhone(bus)
    seen = []
    bus.on(msg_type + ".response", lambda m: seen.append(m.data))
    data = {"action": A + "SET_ALARM", "name": "apps.list"}
    if bad_id is not None:
        data["id"] = bad_id
    bus.emit(origin().reply(msg_type, data))
    assert seen == [{"id": "", "ok": False, "error": "bad_request",
                     "message": phone.responses[0].message}]


# --- routing -----------------------------------------------------------------

def test_destination_filtering_two_phones(bus, client):
    alice = FakePhone(bus, peer="alice")
    bob = FakePhone(bus, peer="bob", launch=ErrorCode.NO_HANDLER)
    assert send(client, make_intent("SET_ALARM"), peer="alice").ok
    assert send(client, make_intent("SET_ALARM"), peer="bob").error is ErrorCode.NO_HANDLER
    assert len(alice.log) == 1 and len(bob.log) == 1
    assert send(client, make_intent("SET_ALARM"), peer="carol", timeout_s=0.05) is None


def test_no_destination_reaches_every_phone(bus):
    alice, bob = FakePhone(bus, peer="alice"), FakePhone(bus, peer="bob")
    bus.emit(Message(INTENT, make_intent("SET_ALARM").to_dict()))
    assert len(alice.log) == 1 and len(bob.log) == 1


def test_responses_come_from_the_peer(bus, client):
    FakePhone(bus, peer="alice")
    seen = []
    bus.on(INTENT_RESPONSE, seen.append)
    send(client, make_intent("SET_ALARM"), peer="alice")
    assert seen[0].context["source"] == "alice"
    assert seen[0].context["destination"] == "skills"


def test_announce(bus):
    phone = FakePhone(bus, peer="alice")
    seen = []
    bus.on(CAPABILITIES, seen.append)
    phone.announce(Message("x", {}, {"session": {"session_id": "s1"}}))
    assert Capabilities.from_dict(seen[0].data) == DEFAULT_CAPABILITIES
    assert seen[0].context["source"] == "alice"
    assert seen[0].context["session"] == {"session_id": "s1"}
    # A reply to the announcement is addressed to the phone, as HiveMind routes it.
    assert seen[0].reply(INTENT, {}).context["destination"] == "alice"


def test_default_capabilities_cover_wiggins_defaults():
    actions = {r.action for r in DEFAULT_CAPABILITIES.rules}
    assert {A + "SET_ALARM", A + "SET_TIMER", A + "SHOW_ALARMS", A + "MAIN", A + "DIAL",
            A + "SENDTO"} <= actions
    assert DEFAULT_CAPABILITIES.unmatched is Mode.BLOCK
    assert DEFAULT_CAPABILITIES.ask_timeout_s == 15


def test_close_removes_handlers_and_pending_answers(bus, client):
    phone = FakePhone(bus, response_delay_s=0.05)
    assert send(client, make_intent("SET_ALARM"), timeout_s=0.01) is None
    phone.close()
    assert send(client, make_intent("SET_ALARM"), timeout_s=0.1) is None
    assert phone.log == []
    assert bus.ee.listeners(INTENT) == [] and bus.ee.listeners(QUERY) == []


# --- queries -----------------------------------------------------------------

EVENTS = [
    {"summary": "Later", "dtstart": "2026-10-09T15:00:00Z", "dtend": "2026-10-09T16:00:00Z",
     "all_day": False},
    {"summary": "Past", "dtstart": "2026-10-04T15:00:00Z", "dtend": "2026-10-04T16:00:00Z",
     "all_day": False},
    {"summary": "Dentist", "dtstart": "2026-10-05T19:00:00Z", "dtend": "2026-10-05T20:00:00Z",
     "all_day": False, "location": "123 Main St", "calendar": "Personal"},
    {"summary": "Ongoing", "dtstart": "2026-10-05T11:00:00Z", "dtend": "2026-10-05T13:00:00Z",
     "all_day": False},
    {"summary": "Holiday", "dtstart": "2026-10-06", "dtend": "2026-10-07", "all_day": True},
    {"summary": "Far", "dtstart": "2026-10-20T15:00:00Z", "dtend": "2026-10-20T16:00:00Z",
     "all_day": False},
]

CONTACTS = [
    {"fn": "Mom", "nickname": "Mother", "tel": [{"value": "+15551234567", "type": "cell"}]},
    {"fn": "Sam Lee", "tel": [{"value": "+15550000001", "type": "work"}]},
    {"fn": "Sam Ortiz", "nickname": ["Sammy"], "tel": [{"value": "+15550000002", "type": "home"}]},
    {"fn": "Jenny", "tel": []},
]

APPS = [{"label": "Camera", "package": "app.grapheneos.camera"},
        {"label": "Settings", "package": "com.android.settings"},
        {"label": "Calendar", "package": "org.fossify.calendar"}]


@pytest.fixture
def phone(bus):
    return FakePhone(bus, events=EVENTS, contacts=CONTACTS, apps=APPS, now=lambda: NOW)


def summaries(response):
    return [e["summary"] for e in response.data["events"]]


def test_calendar_next_default_count(phone, client):
    assert summaries(query(client, "calendar.next")) == ["Ongoing"]


def test_calendar_next_order_count_and_window(phone, client):
    response = query(client, "calendar.next", count=10)
    assert summaries(response) == ["Ongoing", "Dentist", "Holiday", "Later"]
    assert response.data["events"][1] == EVENTS[2]
    assert summaries(query(client, "calendar.next", count=10, within_days=1)) == \
        ["Ongoing", "Dentist", "Holiday"]  # the holiday starts at midnight Chicago time
    assert summaries(query(client, "calendar.next", count=10, within_days=30))[-1] == "Far"


def test_calendar_now_can_be_a_datetime(bus, client):
    FakePhone(bus, events=EVENTS, now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert summaries(query(client, "calendar.next", count=5)) == ["Far"]


def test_contacts_lookup_matches_fn_and_nickname(phone, client):
    response = query(client, "contacts.lookup", name="mother")
    assert response.data == {"contacts": [{"fn": "Mom", "tel": [{"value": "+15551234567",
                                                                 "type": "cell"}]}]}
    assert [c["fn"] for c in query(client, "contacts.lookup", name="SAM").data["contacts"]] == \
        ["Sam Lee", "Sam Ortiz"]
    assert [c["fn"] for c in query(client, "contacts.lookup", name="sammy").data["contacts"]] == \
        ["Sam Ortiz"]
    assert query(client, "contacts.lookup", name="sam", limit=1).data["contacts"][0]["fn"] == "Sam Lee"
    assert query(client, "contacts.lookup", name="Nobody").data == {"contacts": []}


def test_apps_list_filter_and_limit(phone, client):
    assert query(client, "apps.list").data == {"apps": APPS}
    assert query(client, "apps.list", name="CA").data["apps"] == [APPS[0], APPS[2]]
    assert query(client, "apps.list", limit=1).data["apps"] == [APPS[0]]


def test_query_not_enabled_is_blocked(bus, client):
    caps = Capabilities.from_dict({**DEFAULT_CAPABILITIES.to_dict(), "queries": ["apps.list"]})
    FakePhone(bus, capabilities=caps)
    assert query(client, "contacts.lookup", name="Mom").error is ErrorCode.BLOCKED
    assert query(client, "apps.list").ok


def test_permission_denied(bus, client):
    FakePhone(bus, permission_denied={"calendar.next"})
    assert query(client, "calendar.next").error is ErrorCode.PERMISSION_DENIED
    assert query(client, "contacts.lookup", name="x").ok


def test_query_silent_then_answering(bus, client):
    phone = FakePhone(bus, apps=APPS, silent=True)
    assert query(client, "apps.list", timeout_s=0.05) is None
    phone.silent = False
    assert query(client, "apps.list").ok
