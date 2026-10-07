"""The S3 requests with the fake phone: calendar, apps, calls and texts, with "which one?"."""
from __future__ import annotations

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.requests import REQUEST, REQUEST_RESPONSE, RequestResponse
from waggle.fake_phone import AskAnswer
from waggle.intents import ACTION_DIAL, ACTION_MAIN, ACTION_SENDTO, EXTRA_SMS_BODY
from waggle.messages import APPS_LIST, CALENDAR_NEXT, CONTACTS_LOOKUP, INTENT, QUERY

from conftest import NOW, PEER, capabilities

MOM = {"fn": "Mom", "tel": [{"value": "+15550001111", "type": "cell"},
                            {"value": "+15550002222", "type": "home"}]}
SAM_LEE = {"fn": "Sam Lee", "tel": [{"value": "+15550003333", "type": "cell"}]}
SAM_ORTIZ = {"fn": "Sam Ortiz", "tel": [{"value": "+15550004444", "type": "work"}]}
NO_NUMBER = {"fn": "Pat", "tel": []}
CONTACTS = [MOM, SAM_LEE, SAM_ORTIZ, NO_NUMBER]
APPS = [{"label": "Camera", "package": "app.grapheneos.camera"},
        {"label": "Maps", "package": "app.organicmaps"},
        {"label": "Google Maps", "package": "com.google.android.apps.maps"},
        {"label": "Spotify: Music and Podcasts", "package": "com.spotify.music"}]
# NOW is 14:10 in Chicago on Wednesday 7 October; these are in UTC.
DENTIST = {"summary": "Dentist", "dtstart": "2026-10-08T19:00:00Z",
           "dtend": "2026-10-08T20:00:00Z", "all_day": False, "location": "123 Main St"}
SOCCER = {"summary": "Soccer", "dtstart": "2026-10-09T22:00:00Z",
          "dtend": "2026-10-09T23:00:00Z", "all_day": False}
HOLIDAY = {"summary": "Holiday", "dtstart": "2026-10-09", "dtend": "2026-10-10", "all_day": True}
STANDUP = {"summary": "Stand-up", "dtstart": "2026-10-08T14:30:00Z",
           "dtend": "2026-10-08T14:45:00Z", "all_day": False}
EVENTS = [DENTIST, SOCCER, HOLIDAY, STANDUP]


class Script:
    """Stands in for get_response: answers each question in turn, recording what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[tuple[str, dict, Message]] = []

    def __call__(self, origin, dialog, data):
        self.asked.append((dialog, data, origin))
        return self.answers.pop(0) if self.answers else None

    @property
    def dialogs(self):
        return [d for d, _, _ in self.asked]


@pytest.fixture
def script(pipeline):
    def install(*answers):
        s = Script(*answers)
        pipeline.handlers.ask = s
        return s
    return install


@pytest.fixture
def full_phone(pipeline, make_phone):
    return make_phone(contacts=CONTACTS, apps=APPS, events=EVENTS, now=NOW)


def request(name, params, speak=True):
    return Message(REQUEST, {"request": name, "params": params, "speak": speak},
                   {"source": PEER, "peer": PEER, "destination": "skills"})


def run(bus, recorder, name, params, speak=True):
    recorder.clear()
    bus.emit(request(name, params, speak))
    responses = recorder.of(REQUEST_RESPONSE)
    assert len(responses) == 1
    return RequestResponse.from_dict(responses[0].data)


def sent(recorder, msg_type=INTENT):
    return [m.data for m in recorder.of(msg_type)]


# --- calendar.next -----------------------------------------------------------

def test_next_event_with_its_location(bus, recorder, pipeline, make_phone):
    make_phone(events=[SOCCER, DENTIST], now=NOW)
    response = run(bus, recorder, "calendar.next", {"count": 1})
    assert response == RequestResponse.success(
        "Next on your calendar: Dentist, tomorrow at 2 PM, at 123 Main St.")
    query = sent(recorder, QUERY)[0]
    assert query["name"] == CALENDAR_NEXT and query["params"] == {"count": 1, "within_days": 7}
    assert sent(recorder) == []  # a query only, no intent


def test_next_events(bus, recorder, full_phone):
    response = run(bus, recorder, "calendar.next", {"count": 3})
    assert response.summary == ("Coming up: Stand-up, tomorrow at 9:30 AM; Dentist, tomorrow "
                                "at 2 PM; and Holiday, all day Friday.")


def test_default_is_one_event(bus, recorder, full_phone):
    run(bus, recorder, "calendar.next", {})
    assert sent(recorder, QUERY)[0]["params"]["count"] == 1


def test_a_day(bus, recorder, full_phone):
    response = run(bus, recorder, "calendar.next", {"day": "2026-10-09"})
    assert response.summary == "On your calendar on Friday: Holiday, all day; and Soccer at 5 PM."
    assert sent(recorder, QUERY)[0]["params"] == {"count": 10, "within_days": 3}


def test_tomorrow(bus, recorder, full_phone):
    response = run(bus, recorder, "calendar.next", {"day": "2026-10-08"})
    assert response.summary == ("On your calendar tomorrow: Stand-up at 9:30 AM and "
                                "Dentist at 2 PM.")


@pytest.mark.parametrize("params,said", [
    ({"day": "2026-10-07"}, "There's nothing on your calendar today."),
    ({}, "There's nothing on your calendar in the next week."),
])
def test_nothing(bus, recorder, pipeline, make_phone, params, said):
    make_phone(events=[], now=NOW)
    assert run(bus, recorder, "calendar.next", params) == RequestResponse.success(said)


def test_calendar_not_shared(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(queries=[APPS_LIST]), events=EVENTS, now=NOW)
    response = run(bus, recorder, "calendar.next", {})
    assert response == RequestResponse.failure(
        "blocked", "Your phone doesn't share your calendar with the hub.")
    assert sent(recorder, QUERY) == []  # never asked


def test_calendar_permission_denied(bus, recorder, pipeline, make_phone):
    make_phone(events=EVENTS, now=NOW, permission_denied={CALENDAR_NEXT})
    response = run(bus, recorder, "calendar.next", {})
    assert response == RequestResponse.failure(
        "permission_denied", "Your phone doesn't have permission for that.")


def test_calendar_unreachable(bus, recorder, pipeline, make_phone):
    make_phone(silent=True)
    response = run(bus, recorder, "calendar.next", {})
    assert response == RequestResponse.failure("unreachable", "I couldn't reach your phone.")


# --- app.open ----------------------------------------------------------------

def test_open_an_app(bus, recorder, full_phone, script):
    s = script()
    response = run(bus, recorder, "app.open", {"name": "camera"})
    assert response == RequestResponse.success("Opening Camera.")
    assert s.asked == []
    query = sent(recorder, QUERY)[0]
    assert query["name"] == APPS_LIST and query["params"]["name"] == "camera"
    intent = sent(recorder)[0]
    assert intent["action"] == ACTION_MAIN and intent["package"] == "app.grapheneos.camera"


def test_exact_name_wins(bus, recorder, full_phone, script):
    s = script()
    response = run(bus, recorder, "app.open", {"name": "Maps"})  # matches Maps and Google Maps
    assert response.summary == "Opening Maps."
    assert s.asked == []


def test_which_app(bus, recorder, full_phone, script):
    s = script("the first one")
    response = run(bus, recorder, "app.open", {"name": "map"})
    assert s.asked[0][:2] == ("which_app", {"options": "Maps or Google Maps"})
    assert response.summary == "Opening Maps."
    assert sent(recorder)[0]["package"] == "app.organicmaps"


def test_which_app_cancelled(bus, recorder, full_phone, script):
    script(None)
    response = run(bus, recorder, "app.open", {"name": "map"})
    assert response == RequestResponse.failure("declined", "Okay, cancelled.")
    assert sent(recorder) == []


def test_which_app_not_understood_twice(bus, recorder, full_phone, script):
    s = script("bananas", "still bananas")
    response = run(bus, recorder, "app.open", {"name": "map"})
    assert s.dialogs == ["which_app", "pick_again"]
    assert response == RequestResponse.failure("declined", "Sorry, I didn't catch that.")


def test_which_app_second_try(bus, recorder, full_phone, script):
    s = script("bananas", "google")
    response = run(bus, recorder, "app.open", {"name": "map"})
    assert s.dialogs == ["which_app", "pick_again"]
    assert response.summary == "Opening Google Maps."


def test_no_such_app(bus, recorder, full_phone):
    response = run(bus, recorder, "app.open", {"name": "garage door"})
    assert response == RequestResponse.failure("no_handler",
                                               "I couldn't find an app called garage door.")


def test_app_launch_blocked_by_a_package_rule(bus, recorder, pipeline, make_phone):
    make_phone(apps=APPS, capabilities=capabilities(rules=[
        {"action": ACTION_MAIN, "category": "android.intent.category.LAUNCHER", "mode": "run"},
        {"action": ACTION_MAIN, "package": "app.grapheneos.camera", "mode": "block"}]))
    response = run(bus, recorder, "app.open", {"name": "camera"})
    assert response == RequestResponse.failure("blocked", "Your phone doesn't allow that.")
    assert sent(recorder) == []


def test_question_goes_to_the_phone(bus, recorder, full_phone, script):
    s = script("Maps")
    run(bus, recorder, "app.open", {"name": "map"})
    origin = s.asked[0][2]
    assert origin.reply("speak", {}).context["destination"] == PEER


# --- contact.call ------------------------------------------------------------

def test_call_the_only_number(bus, recorder, full_phone, script):
    s = script()
    response = run(bus, recorder, "contact.call", {"name": "Sam Lee"})
    assert response == RequestResponse.success("Dialing Sam Lee. Tap call to connect.")
    assert s.asked == []
    assert sent(recorder, QUERY)[0]["name"] == CONTACTS_LOOKUP
    intent = sent(recorder)[0]
    assert intent["action"] == ACTION_DIAL and intent["data"] == "tel:+15550003333"
    # DIAL is an "ask" rule: the phone confirms first.
    assert [m.data["utterance"] for m in recorder.of("speak")][0] == "Confirm on your phone."


def test_which_contact(bus, recorder, full_phone, script):
    s = script("Ortiz")
    response = run(bus, recorder, "contact.call", {"name": "sam"})
    assert s.asked[0][:2] == ("which_contact", {"name": "Sam", "options": "Sam Lee or Sam Ortiz"})
    assert response.summary == "Dialing Sam Ortiz. Tap call to connect."
    assert sent(recorder)[0]["data"] == "tel:+15550004444"


def test_which_number(bus, recorder, full_phone, script):
    s = script("home")
    response = run(bus, recorder, "contact.call", {"name": "mom"})
    assert s.asked[0][:2] == ("which_number", {"name": "Mom", "options": "cell or home"})
    assert sent(recorder)[0]["data"] == "tel:+15550002222"
    assert response.ok


def test_number_type_said(bus, recorder, full_phone, script):
    s = script()
    run(bus, recorder, "contact.call", {"name": "mom", "type": "cell"})
    assert s.asked == []
    assert sent(recorder)[0]["data"] == "tel:+15550001111"


def test_no_number_of_that_type(bus, recorder, full_phone):
    response = run(bus, recorder, "contact.call", {"name": "mom", "type": "work"})
    assert response == RequestResponse.failure("no_handler", "Mom has no work number.")


def test_no_number(bus, recorder, full_phone):
    response = run(bus, recorder, "contact.call", {"name": "pat"})
    assert response == RequestResponse.failure("no_handler", "Pat has no phone number.")


def test_no_such_contact(bus, recorder, full_phone):
    response = run(bus, recorder, "contact.call", {"name": "the queen"})
    assert response == RequestResponse.failure("no_handler",
                                               "I couldn't find the queen in your contacts.")


def test_call_a_number_said_aloud(bus, recorder, full_phone):
    response = run(bus, recorder, "contact.call", {"name": "555 1234"})
    assert sent(recorder, QUERY) == []
    assert sent(recorder)[0]["data"] == "tel:5551234"
    assert response.summary == "Dialing 555 1234. Tap call to connect."


def test_call_declined_on_the_phone(bus, recorder, pipeline, make_phone):
    make_phone(contacts=CONTACTS, ask_answer=AskAnswer.DECLINE)
    response = run(bus, recorder, "contact.call", {"name": "Sam Lee"})
    assert response == RequestResponse.failure("declined", "Okay, cancelled.")


def test_contacts_not_shared(bus, recorder, pipeline, make_phone):
    make_phone(contacts=CONTACTS, capabilities=capabilities(queries=[CALENDAR_NEXT]))
    response = run(bus, recorder, "contact.call", {"name": "mom"})
    assert response.summary == "Your phone doesn't share your contacts with the hub."


def test_two_contacts_with_the_same_name(bus, recorder, pipeline, make_phone, script):
    s = script()
    twin = {"fn": "Sam Lee", "tel": [{"value": "+15559999999", "type": "cell"}]}
    make_phone(contacts=[SAM_LEE, twin])
    run(bus, recorder, "contact.call", {"name": "sam lee"})
    assert s.asked == []  # can't be told apart by voice: the first
    assert sent(recorder)[0]["data"] == "tel:+15550003333"


def test_duplicate_types_say_the_last_digits(bus, recorder, pipeline, make_phone, script):
    s = script("ending in 2 2 2 2")
    two_cells = {"fn": "Kim", "tel": [{"value": "+15551111111", "type": "cell"},
                                      {"value": "+15552222222", "type": "cell"}]}
    make_phone(contacts=[two_cells])
    run(bus, recorder, "contact.call", {"name": "kim"})
    assert s.asked[0][1]["options"] == "cell ending in 1 1 1 1 or cell ending in 2 2 2 2"
    assert sent(recorder)[0]["data"] == "tel:+15552222222"


# --- message.compose ---------------------------------------------------------

def test_compose(bus, recorder, full_phone, script):
    s = script()
    response = run(bus, recorder, "message.compose", {"name": "mom", "body": "I'm late!"})
    assert response == RequestResponse.success("Message to Mom ready. Tap send on your phone.")
    assert s.asked == []  # texts go to the one cell number without asking
    intent = sent(recorder)[0]
    assert intent["action"] == ACTION_SENDTO and intent["data"] == "smsto:+15550001111"
    assert intent["extras"][EXTRA_SMS_BODY] == {"type": "string", "value": "I'm late!"}


def test_compose_asks_without_a_cell(bus, recorder, pipeline, make_phone, script):
    s = script("work")
    make_phone(contacts=[{"fn": "Lou", "tel": [{"value": "+15551", "type": "home"},
                                               {"value": "+15552", "type": "work"}]}])
    run(bus, recorder, "message.compose", {"name": "lou", "body": "hi"})
    assert s.dialogs == ["which_number"]
    assert sent(recorder)[0]["data"] == "smsto:+15552"


def test_compose_without_speaking(bus, recorder, full_phone):
    response = run(bus, recorder, "message.compose", {"name": "mom", "body": "hi"}, speak=False)
    assert response.ok
    assert recorder.of("speak") == []
