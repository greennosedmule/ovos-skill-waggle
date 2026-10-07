"""Waggle query builders and result models (see WAGGLE.md "waggle.query").

The builders make :class:`~waggle.messages.Query` requests for the hub. The
models parse a successful response's ``data`` (``parse_*``) and, for the
phone side and fakes, produce it (``*_data``). Parsing ignores unknown fields
and raises ``bad_request`` for anything malformed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT, CONTACTS_LOOKUP, Query, _drop_none, _require_dict, _str,
    bad_request, from_wire_time, new_request_id, to_wire_time,
)

PHONE_TYPES = frozenset({"cell", "home", "work", "other"})


# --- builders ----------------------------------------------------------------

def _query(name: str, params: dict, id: Optional[str]) -> Query:
    return Query(id=new_request_id() if id is None else id, name=name, params=params)


def calendar_next(count: int = 1, within_days: int = 7, *, id: Optional[str] = None) -> Query:
    """The next ``count`` (1–10) events starting within ``within_days`` days."""
    return _query(CALENDAR_NEXT, {"count": count, "within_days": within_days}, id)


def contacts_lookup(name: str, limit: int = 5, *, id: Optional[str] = None) -> Query:
    """Contacts whose display name or nickname matches ``name``."""
    return _query(CONTACTS_LOOKUP, {"name": name, "limit": limit}, id)


def apps_list(name: Optional[str] = None, limit: int = 20, *, id: Optional[str] = None) -> Query:
    """Launchable apps, optionally only those whose label matches ``name``."""
    params = _drop_none({"name": name, "limit": limit})
    return _query(APPS_LIST, params, id)


# --- result models -----------------------------------------------------------

def _list(data: Any, key: str) -> list:
    value = _require_dict(data, "query result").get(key)
    if not isinstance(value, list):
        raise bad_request(f"{key!r} must be a list")
    return value


def _is_datetime(value: Any) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None


def _is_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


@dataclass(frozen=True)
class CalendarEvent:
    """One ``calendar.next`` event. Timed events use aware datetimes, all-day
    events dates, with ``dtend`` exclusive as in iCalendar."""
    summary: str
    dtstart: datetime | date
    dtend: Optional[datetime | date] = None
    all_day: bool = False
    location: Optional[str] = None
    calendar: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.summary, str):
            raise bad_request("'summary' must be a string")
        if not isinstance(self.all_day, bool):
            raise bad_request("'all_day' must be a boolean")
        is_kind = _is_date if self.all_day else _is_datetime
        kind = "date" if self.all_day else "aware datetime"
        for name in ("dtstart", "dtend"):
            value = getattr(self, name)
            if not (value is None and name == "dtend") and not is_kind(value):
                raise bad_request(f"{name!r} must be a {kind} when all_day is {self.all_day}")
        if self.dtend is not None and self.dtend < self.dtstart:
            raise bad_request("'dtend' is before 'dtstart'")
        for name in ("location", "calendar"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise bad_request(f"{name!r} must be a string")

    @classmethod
    def from_dict(cls, d: Any) -> "CalendarEvent":
        d = _require_dict(d, "event")
        if "dtstart" not in d:
            raise bad_request("missing 'dtstart'")
        dtstart = from_wire_time(d["dtstart"])
        dtend = d.get("dtend")
        # all_day is in every event WAGGLE.md shows; if a phone leaves it out,
        # the shape of dtstart says which it is.
        all_day = d.get("all_day", _is_date(dtstart))
        return cls(summary=d.get("summary"), dtstart=dtstart,
                   dtend=None if dtend is None else from_wire_time(dtend),
                   all_day=all_day, location=_str(d, "location"), calendar=_str(d, "calendar"))

    def to_dict(self) -> dict:
        return _drop_none({
            "summary": self.summary, "dtstart": to_wire_time(self.dtstart),
            "dtend": None if self.dtend is None else to_wire_time(self.dtend),
            "all_day": self.all_day, "location": self.location, "calendar": self.calendar,
        })


@dataclass(frozen=True)
class Phone:
    """A contact's phone number; ``type`` is cell, home, work or other."""
    value: str
    type: str

    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value:
            raise bad_request("phone 'value' must be a non-empty string")
        if self.type not in PHONE_TYPES:
            raise bad_request(f"bad phone type {self.type!r}")

    @classmethod
    def from_dict(cls, d: Any) -> "Phone":
        d = _require_dict(d, "phone")
        return cls(value=_str(d, "value", required=True), type=d.get("type"))

    def to_dict(self) -> dict:
        return {"value": self.value, "type": self.type}


@dataclass(frozen=True)
class Contact:
    """A ``contacts.lookup`` match: its display name and phone numbers."""
    fn: str
    tel: tuple[Phone, ...] = ()

    def __post_init__(self):
        if not isinstance(self.fn, str) or not self.fn:
            raise bad_request("contact 'fn' must be a non-empty string")
        object.__setattr__(self, "tel", tuple(self.tel))
        if not all(isinstance(p, Phone) for p in self.tel):
            raise bad_request("contact 'tel' must hold Phone values")

    @classmethod
    def from_dict(cls, d: Any) -> "Contact":
        d = _require_dict(d, "contact")
        tel = d.get("tel", [])
        if not isinstance(tel, list):
            raise bad_request("'tel' must be a list")
        return cls(fn=_str(d, "fn", required=True), tel=tuple(Phone.from_dict(p) for p in tel))

    def to_dict(self) -> dict:
        return {"fn": self.fn, "tel": [p.to_dict() for p in self.tel]}


@dataclass(frozen=True)
class App:
    """An ``apps.list`` entry: a launcher app's label and package."""
    label: str
    package: str

    def __post_init__(self):
        for name in ("label", "package"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise bad_request(f"app {name!r} must be a non-empty string")

    @classmethod
    def from_dict(cls, d: Any) -> "App":
        d = _require_dict(d, "app")
        return cls(label=_str(d, "label", required=True), package=_str(d, "package", required=True))

    def to_dict(self) -> dict:
        return {"label": self.label, "package": self.package}


# --- response data -----------------------------------------------------------

def parse_calendar_next(data: Any) -> list[CalendarEvent]:
    return [CalendarEvent.from_dict(e) for e in _list(data, "events")]


def parse_contacts(data: Any) -> list[Contact]:
    return [Contact.from_dict(c) for c in _list(data, "contacts")]


def parse_apps(data: Any) -> list[App]:
    return [App.from_dict(a) for a in _list(data, "apps")]


def calendar_data(events: Iterable[CalendarEvent]) -> dict:
    return {"events": [e.to_dict() for e in events]}


def contacts_data(contacts: Iterable[Contact]) -> dict:
    return {"contacts": [c.to_dict() for c in contacts]}


def apps_data(apps: Iterable[App]) -> dict:
    return {"apps": [a.to_dict() for a in apps]}
