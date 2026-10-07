"""Calendar events in words, on the phone's clock (see SPEC.md "Requests", ``calendar.next``).

Event times come from the phone in UTC and are spoken in the phone's
timezone: "Dentist, tomorrow at 2 PM", "Holiday, all day Friday",
"Stand-up, now until 10:15 AM". Days within the coming week are named
("today", "tomorrow", "Friday"); later ones get a date ("on October 15").
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Iterable

from waggle.queries import CalendarEvent

UNTITLED = "Untitled event"


def spoken_time(when: datetime) -> str:
    """ "2 PM", "2:30 PM", "12 AM"."""
    hour = when.hour % 12 or 12
    suffix = "AM" if when.hour < 12 else "PM"
    return f"{hour} {suffix}" if when.minute == 0 else f"{hour}:{when.minute:02d} {suffix}"


def spoken_day(day: date, today: date) -> str:
    """ "today", "tomorrow", "on Friday" (within a week), "on October 15"."""
    ahead = (day - today).days
    if ahead == 0:
        return "today"
    if ahead == 1:
        return "tomorrow"
    if 1 < ahead < 7:
        return f"on {day.strftime('%A')}"
    return f"on {day.strftime('%B')} {day.day}"


def spoken_list(items: list[str]) -> str:
    """ "A", "A and B", "A; B; and C" (semicolons, since items have commas)."""
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2 and not any("," in i for i in items):
        return f"{items[0]} and {items[1]}"
    return "; ".join(items[:-1]) + f"; and {items[-1]}"


def _local(value: datetime, tz: tzinfo) -> datetime:
    return value.astimezone(tz)


def bounds(event: CalendarEvent, tz: tzinfo) -> tuple[datetime, datetime]:
    """The event's start and (exclusive) end as aware datetimes on the phone's clock."""
    if event.all_day:
        start = datetime.combine(event.dtstart, time(), tzinfo=tz)
        end_day = event.dtend if event.dtend is not None else event.dtstart + timedelta(days=1)
        return start, datetime.combine(end_day, time(), tzinfo=tz)
    start = _local(event.dtstart, tz)
    end = _local(event.dtend, tz) if event.dtend is not None else start
    return start, end


def on_day(events: Iterable[CalendarEvent], day: date, tz: tzinfo) -> list[CalendarEvent]:
    """The events that take place, at least partly, on ``day``."""
    day_start = datetime.combine(day, time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    found = []
    for event in events:
        start, end = bounds(event, tz)
        if start < day_end and (end > day_start or (end == start and start >= day_start)):
            found.append(event)
    return found


def spoken_event(event: CalendarEvent, now: datetime, tz: tzinfo, with_day: bool = True,
                 with_location: bool = False) -> str:
    """One event in words; ``with_day`` False leaves out the day (it's already been said)."""
    summary = event.summary.strip() or UNTITLED
    now = now.astimezone(tz)
    start, end = bounds(event, tz)
    if event.all_day:
        if not with_day:
            text = f"{summary}, all day"
        elif start.date() <= now.date():
            text = f"{summary}, all day today"
        else:
            text = f"{summary}, all day {spoken_day(start.date(), now.date()).removeprefix('on ')}"
    elif start <= now < end:
        text = f"{summary}, now until {spoken_time(end)}"
    elif with_day:
        text = f"{summary}, {spoken_day(start.date(), now.date())} at {spoken_time(start)}"
    else:
        text = f"{summary} at {spoken_time(start)}"
    if with_location and event.location and event.location.strip():
        text += f", at {event.location.strip()}"
    return text

