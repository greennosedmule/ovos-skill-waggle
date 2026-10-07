"""Calendar events in words, on the phone's clock."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ovos_skill_waggle import agenda
from waggle.queries import CalendarEvent

CHICAGO = ZoneInfo("America/Chicago")
# 14:10 in Chicago on Wednesday 7 October 2026.
NOW = datetime(2026, 10, 7, 19, 10, tzinfo=timezone.utc)
TODAY = date(2026, 10, 7)


def timed(summary, start, end=None, **kw):
    return CalendarEvent(summary=summary, dtstart=start, dtend=end, **kw)


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour,minute,said", [(14, 0, "2 PM"), (14, 30, "2:30 PM"),
                                              (0, 0, "12 AM"), (12, 5, "12:05 PM")])
def test_spoken_time(hour, minute, said):
    assert agenda.spoken_time(datetime(2026, 1, 1, hour, minute)) == said


@pytest.mark.parametrize("day,said", [(7, "today"), (8, "tomorrow"), (9, "on Friday"),
                                      (13, "on Tuesday"), (14, "on October 14"),
                                      (31, "on October 31")])
def test_spoken_day(day, said):
    assert agenda.spoken_day(date(2026, 10, day), TODAY) == said


def test_spoken_list():
    assert agenda.spoken_list(["A"]) == "A"
    assert agenda.spoken_list(["A", "B"]) == "A and B"
    assert agenda.spoken_list(["A, today", "B"]) == "A, today; and B"
    assert agenda.spoken_list(["A", "B", "C"]) == "A; B; and C"


@pytest.mark.parametrize("event,said", [
    # 19:00 UTC tomorrow is 2 PM in Chicago.
    (timed("Dentist", utc(8, 19), utc(8, 20)), "Dentist, tomorrow at 2 PM"),
    (timed("Stand-up", utc(7, 23, 30), utc(8, 0)), "Stand-up, today at 6:30 PM"),
    (timed("Review", utc(7, 19), utc(7, 20, 15)), "Review, now until 3:15 PM"),
    (timed("Book club", utc(9, 0)), "Book club, tomorrow at 7 PM"),
    (timed("Trip", utc(15, 13)), "Trip, on October 15 at 8 AM"),
    (timed("", utc(8, 19)), "Untitled event, tomorrow at 2 PM"),
    (timed("Holiday", date(2026, 10, 9), date(2026, 10, 10), all_day=True),
     "Holiday, all day Friday"),
    (timed("Conference", date(2026, 10, 6), date(2026, 10, 9), all_day=True),
     "Conference, all day today"),
])
def test_spoken_event(event, said):
    assert agenda.spoken_event(event, NOW, CHICAGO) == said


def test_spoken_event_without_day_and_with_location():
    event = timed("Dentist", utc(8, 19), location=" 123 Main St ")
    assert agenda.spoken_event(event, NOW, CHICAGO, with_day=False) == "Dentist at 2 PM"
    assert (agenda.spoken_event(event, NOW, CHICAGO, with_location=True)
            == "Dentist, tomorrow at 2 PM, at 123 Main St")


def test_on_day_uses_the_phones_midnight():
    events = [
        timed("Late", utc(8, 4)),                       # 11 PM on the 7th in Chicago
        timed("Early", utc(8, 6)),                      # 1 AM on the 8th
        timed("Spans midnight", utc(8, 4), utc(8, 7)),  # 11 PM to 2 AM
        timed("Holiday", date(2026, 10, 8), date(2026, 10, 9), all_day=True),
        timed("Week", date(2026, 10, 5), date(2026, 10, 12), all_day=True),
    ]
    names = [e.summary for e in agenda.on_day(events, date(2026, 10, 8), CHICAGO)]
    assert names == ["Early", "Spans midnight", "Holiday", "Week"]
