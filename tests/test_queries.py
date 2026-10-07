from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from waggle import queries
from waggle.messages import ErrorCode, Query, WaggleError
from waggle.queries import App, CalendarEvent, Contact, Phone

ID = "q-1"
UTC = timezone.utc


def wire(query: Query) -> dict:
    d = query.to_dict()
    assert Query.from_dict(d) == query
    return d


def assert_bad_request(fn, *args):
    with pytest.raises(WaggleError) as e:
        fn(*args)
    assert e.value.code is ErrorCode.BAD_REQUEST


# --- builders ----------------------------------------------------------------

def test_calendar_next_wire():
    assert wire(queries.calendar_next(id=ID)) == {
        "id": ID, "name": "calendar.next", "params": {"count": 1, "within_days": 7}}
    assert wire(queries.calendar_next(3, 14, id=ID))["params"] == {"count": 3, "within_days": 14}


def test_contacts_lookup_wire():
    assert wire(queries.contacts_lookup("Mom", id=ID)) == {
        "id": ID, "name": "contacts.lookup", "params": {"name": "Mom", "limit": 5}}
    assert wire(queries.contacts_lookup("Sam", limit=2, id=ID))["params"] == {"name": "Sam", "limit": 2}


def test_apps_list_wire():
    assert wire(queries.apps_list(id=ID)) == {"id": ID, "name": "apps.list", "params": {"limit": 20}}
    assert wire(queries.apps_list("cam", 3, id=ID))["params"] == {"name": "cam", "limit": 3}


def test_builders_make_new_ids():
    a, b = queries.apps_list(), queries.apps_list()
    assert a.id != b.id
    uuid.UUID(a.id)


@pytest.mark.parametrize("build", [
    lambda: queries.calendar_next(0),
    lambda: queries.calendar_next(11),
    lambda: queries.calendar_next(True),
    lambda: queries.calendar_next(1, 0),
    lambda: queries.contacts_lookup(""),
    lambda: queries.contacts_lookup(None),
    lambda: queries.contacts_lookup("Mom", 0),
    lambda: queries.apps_list(3),
    lambda: queries.apps_list(limit=0),
    lambda: queries.apps_list(id=""),
])
def test_builder_bad_args(build):
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize("count", [1, 10])
def test_calendar_next_count_bounds(count):
    assert queries.calendar_next(count).params["count"] == count


# --- calendar events ---------------------------------------------------------

DENTIST = {"summary": "Dentist", "dtstart": "2026-10-05T19:00:00Z", "dtend": "2026-10-05T20:00:00Z",
           "all_day": False, "location": "123 Main St", "calendar": "Personal"}


def test_calendar_event_from_waggle_md_example():
    [event] = queries.parse_calendar_next({"events": [DENTIST]})
    assert event == CalendarEvent(
        summary="Dentist", dtstart=datetime(2026, 10, 5, 19, tzinfo=UTC),
        dtend=datetime(2026, 10, 5, 20, tzinfo=UTC), all_day=False,
        location="123 Main St", calendar="Personal")
    assert queries.calendar_data([event]) == {"events": [DENTIST]}


def test_calendar_event_all_day():
    d = {"summary": "Holiday", "dtstart": "2026-12-25", "dtend": "2026-12-26", "all_day": True}
    event = CalendarEvent.from_dict(d)
    assert event.dtstart == date(2026, 12, 25) and event.dtend == date(2026, 12, 26)
    assert event.to_dict() == d


def test_calendar_event_optional_fields_absent():
    d = {"summary": "Call", "dtstart": "2026-10-05T19:00:00Z", "all_day": False}
    event = CalendarEvent.from_dict(d)
    assert (event.dtend, event.location, event.calendar) == (None, None, None)
    assert event.to_dict() == d


def test_calendar_event_unknown_fields_ignored():
    event = CalendarEvent.from_dict({**DENTIST, "rrule": "FREQ=WEEKLY", "x": {}})
    assert event.to_dict() == DENTIST


def test_calendar_event_all_day_inferred_when_absent():
    assert CalendarEvent.from_dict({"summary": "A", "dtstart": "2026-10-05"}).all_day is True
    assert CalendarEvent.from_dict({"summary": "A", "dtstart": "2026-10-05T01:00:00Z"}).all_day is False


def test_calendar_event_empty_summary_allowed():
    assert CalendarEvent.from_dict({**DENTIST, "summary": ""}).summary == ""


def test_calendar_event_serializes_other_zones_as_utc():
    chicago = timezone(timedelta(hours=-5))
    event = CalendarEvent("A", datetime(2026, 10, 5, 14, tzinfo=chicago))
    assert event.to_dict()["dtstart"] == "2026-10-05T19:00:00Z"


@pytest.mark.parametrize("change", [
    {"summary": None}, {"summary": 3},
    {"dtstart": None}, {"dtstart": "2026-10-05T19:00:00"}, {"dtstart": "tomorrow"},
    {"dtstart": 1759690800},
    {"dtend": "2026-10-05T18:00:00Z"},           # before dtstart
    {"dtend": "2026-10-06"},                     # date on a timed event
    {"all_day": True},                           # datetimes on an all-day event
    {"all_day": "false"},
    {"location": 5}, {"calendar": ["Personal"]},
])
def test_calendar_event_malformed(change):
    assert_bad_request(CalendarEvent.from_dict, {**DENTIST, **change})


def test_calendar_event_missing_dtstart():
    d = dict(DENTIST)
    del d["dtstart"]
    assert_bad_request(CalendarEvent.from_dict, d)


def test_calendar_event_all_day_with_datetime_end():
    assert_bad_request(CalendarEvent.from_dict,
                       {"summary": "A", "dtstart": "2026-10-05", "dtend": "2026-10-06T00:00:00Z",
                        "all_day": True})


def test_calendar_event_rejects_naive_datetime():
    assert_bad_request(CalendarEvent, "A", datetime(2026, 10, 5, 19))


@pytest.mark.parametrize("data", [None, [], {}, {"events": None}, {"events": {}},
                                  {"events": ["x"]}, {"events": [DENTIST, None]}])
def test_parse_calendar_next_malformed(data):
    assert_bad_request(queries.parse_calendar_next, data)


def test_parse_calendar_next_empty_and_order():
    assert queries.parse_calendar_next({"events": []}) == []
    later = {**DENTIST, "summary": "Later", "dtstart": "2026-10-06T19:00:00Z", "dtend": None}
    events = queries.parse_calendar_next({"events": [DENTIST, later], "more": True})
    assert [e.summary for e in events] == ["Dentist", "Later"]


# --- contacts ----------------------------------------------------------------

MOM = {"fn": "Mom", "tel": [{"value": "+15551234567", "type": "cell"}]}


def test_contacts_from_waggle_md_example():
    contacts = queries.parse_contacts({"contacts": [MOM]})
    assert contacts == [Contact("Mom", (Phone("+15551234567", "cell"),))]
    assert queries.contacts_data(contacts) == {"contacts": [MOM]}


@pytest.mark.parametrize("type_", ["cell", "home", "work", "other"])
def test_phone_types(type_):
    phone = Phone.from_dict({"value": "1", "type": type_, "label": "x"})
    assert phone.to_dict() == {"value": "1", "type": type_}


def test_contact_round_trip_many_numbers():
    contact = Contact("Sam Lee", [Phone("1", "home"), Phone("2", "work")])
    assert contact.tel == (Phone("1", "home"), Phone("2", "work"))
    assert Contact.from_dict(contact.to_dict()) == contact


def test_contact_without_numbers():
    assert Contact.from_dict({"fn": "Ann"}) == Contact("Ann")
    assert Contact("Ann").to_dict() == {"fn": "Ann", "tel": []}


@pytest.mark.parametrize("contact", [
    None, {}, {"fn": ""}, {"fn": 3}, {"fn": "Mom", "tel": "+1555"},
    {"fn": "Mom", "tel": [{"value": "+1555"}]},
    {"fn": "Mom", "tel": [{"value": "+1555", "type": "mobile"}]},
    {"fn": "Mom", "tel": [{"value": "", "type": "cell"}]},
    {"fn": "Mom", "tel": [{"type": "cell"}]},
    {"fn": "Mom", "tel": ["+1555"]},
])
def test_contact_malformed(contact):
    assert_bad_request(queries.parse_contacts, {"contacts": [contact]})


@pytest.mark.parametrize("data", [None, {}, {"contacts": "Mom"}, {"apps": []}])
def test_parse_contacts_malformed(data):
    assert_bad_request(queries.parse_contacts, data)


# --- apps --------------------------------------------------------------------

CAMERA = {"label": "Camera", "package": "app.grapheneos.camera"}


def test_apps_from_waggle_md_example():
    apps = queries.parse_apps({"apps": [CAMERA, {**CAMERA, "icon": "…"}]})
    assert apps == [App("Camera", "app.grapheneos.camera")] * 2
    assert queries.apps_data(apps[:1]) == {"apps": [CAMERA]}


@pytest.mark.parametrize("app", [
    None, {"label": "Camera"}, {"package": "x"}, {"label": "", "package": "x"},
    {"label": "Camera", "package": 5},
])
def test_app_malformed(app):
    assert_bad_request(queries.parse_apps, {"apps": [app]})


@pytest.mark.parametrize("data", [None, {}, {"apps": None}, {"apps": CAMERA}])
def test_parse_apps_malformed(data):
    assert_bad_request(queries.parse_apps, data)


def test_data_helpers_accept_empty():
    assert queries.calendar_data([]) == {"events": []}
    assert queries.contacts_data([]) == {"contacts": []}
    assert queries.apps_data(()) == {"apps": []}
