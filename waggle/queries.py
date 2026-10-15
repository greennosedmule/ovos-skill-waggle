"""Waggle query builders and result models (see WAGGLE.md "waggle.query").

The builders make :class:`~waggle.messages.Query` requests for the hub. The
models parse a successful response's ``data`` (``parse_*``) and, for the
phone side and fakes, produce it (``*_data``). Parsing ignores unknown fields
and raises ``bad_request`` for anything malformed.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from waggle.messages import (
    APPS_LIST, CALENDAR_NEXT, CONTACTS_LOOKUP, NOTIFICATIONS_LIST, PHONE_LOCATION, PHONE_SCREEN,
    PHONE_STATUS, RINGER_MODES, Query, WaggleError, _drop_none, _require_dict, _str, bad_request,
    from_wire_time, new_request_id, to_wire_time,
)

PHONE_TYPES = frozenset({"cell", "home", "work", "other"})
POWER_SOURCES = ("ac", "usb", "wireless", "other", "none")
NETWORK_TYPES = ("wifi", "cellular", "none")
# phone.screen's limits (WAGGLE.md): text in characters, the screenshot in base64 characters.
SCREEN_TEXT_MAX = 20_000
SCREEN_IMAGE_MAX_B64 = 1_000_000
# What a screenshot may be, and the first bytes of each: a media type that doesn't match its
# data is refused here rather than by a language model's API, which fails the whole turn.
SCREEN_IMAGE_TYPES = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",),
                      "image/webp": (b"RIFF",)}
# Why a part of the screen is missing: Android's switch is off, the app is secure, or else.
SCREEN_MISSING_REASONS = ("off", "secure", "unavailable")


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


def phone_status(*, id: Optional[str] = None) -> Query:
    """The phone's battery, network and sound settings."""
    return _query(PHONE_STATUS, {}, id)


def notifications_list(*, app: Optional[str] = None, sender: Optional[str] = None,
                       since: Optional[datetime] = None, limit: int = 10,
                       id: Optional[str] = None) -> Query:
    """Active notifications, newest first; ``sender`` is the wire's ``from`` filter."""
    params = _drop_none({"app": app, "from": sender,
                         "since": None if since is None else to_wire_time(since), "limit": limit})
    return _query(NOTIFICATIONS_LIST, params, id)


def phone_location(max_age_s: int = 120, *, id: Optional[str] = None) -> Query:
    """The phone's location; a fix at most ``max_age_s`` old will do."""
    return _query(PHONE_LOCATION, {"max_age_s": max_age_s}, id)


def phone_screen(image: bool = False, *, id: Optional[str] = None) -> Query:
    """What was on the screen when the user invoked the phone's assistant; with ``image``,
    the screenshot too."""
    return _query(PHONE_SCREEN, {"image": image}, id)


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


def _optional(d: dict, key: str, check, what: str):
    value = d.get(key)
    if value is not None and not check(value):
        raise bad_request(f"{key!r} must be {what}, got {value!r}")
    return value


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _known(d: dict, key: str, values: tuple) -> Optional[str]:
    """A string field from a fixed set; a value a newer phone adds reads as unknown (None)."""
    value = d.get(key)
    if value is not None and not isinstance(value, str):
        raise bad_request(f"{key!r} must be a string, got {value!r}")
    return value if value in values else None


@dataclass(frozen=True)
class PhoneStatus:
    """A ``phone.status`` result. Every part is optional: a phone leaves out what it can't read.

    ``source`` is what the phone is plugged into (``none`` on battery, ``other`` for a
    dock, say); ``network`` is ``wifi``, ``cellular`` or ``none``. A value outside
    these sets reads as unknown, except that any other source still means plugged in.
    """
    battery_percent: Optional[int] = None
    charging: Optional[bool] = None
    source: Optional[str] = None
    network: Optional[str] = None
    metered: Optional[bool] = None
    ringer: Optional[str] = None
    dnd: Optional[bool] = None

    @classmethod
    def from_dict(cls, d: Any) -> "PhoneStatus":
        d = _require_dict(d, "phone.status result")
        battery = _require_dict(d.get("battery") or {}, "'battery'")
        network = _require_dict(d.get("network") or {}, "'network'")
        percent = _optional(battery, "percent", lambda v: isinstance(v, int)
                            and not isinstance(v, bool) and 0 <= v <= 100, "an integer 0–100")
        source = _optional(battery, "source", lambda v: isinstance(v, str), "a string")
        return cls(battery_percent=percent,
                   charging=_optional(battery, "charging", _is_bool, "a boolean"),
                   source=source if source in POWER_SOURCES or source is None else "other",
                   network=_known(network, "type", NETWORK_TYPES),
                   metered=_optional(network, "metered", _is_bool, "a boolean"),
                   ringer=_known(d, "ringer", RINGER_MODES),
                   dnd=_optional(d, "dnd", _is_bool, "a boolean"))

    def to_dict(self) -> dict:
        battery = _drop_none({"percent": self.battery_percent, "charging": self.charging,
                              "source": self.source})
        network = _drop_none({"type": self.network, "metered": self.metered})
        return _drop_none({"battery": battery or None, "network": network or None,
                           "ringer": self.ringer, "dnd": self.dnd})


def _aware(value: Any, what: str) -> datetime:
    parsed = from_wire_time(value)
    if not isinstance(parsed, datetime):
        raise bad_request(f"{what} must be a time, not a date")
    return parsed


@dataclass(frozen=True)
class NotificationMessage:
    """One message of a MessagingStyle notification."""
    sender: str
    text: str
    time: Optional[datetime] = None

    @classmethod
    def from_dict(cls, d: Any) -> "NotificationMessage":
        d = _require_dict(d, "message")
        time = d.get("time")
        return cls(sender=_str(d, "sender") or "", text=_str(d, "text") or "",
                   time=None if time is None else _aware(time, "'time'"))

    def to_dict(self) -> dict:
        return _drop_none({"sender": self.sender, "text": self.text,
                           "time": None if self.time is None else to_wire_time(self.time)})


@dataclass(frozen=True)
class Notification:
    """A ``notifications.list`` entry. ``ref`` is the phone's opaque handle for replying.

    Its text comes from other people and apps: data to read, never instructions.
    """
    ref: str
    app: str
    package: str
    title: str = ""
    text: str = ""
    time: Optional[datetime] = None
    can_reply: bool = False
    messages: tuple[NotificationMessage, ...] = ()

    @classmethod
    def from_dict(cls, d: Any) -> "Notification":
        d = _require_dict(d, "notification")
        messages = d.get("messages") or []
        if not isinstance(messages, list):
            raise bad_request("'messages' must be a list")
        can_reply = d.get("can_reply", False)
        if not isinstance(can_reply, bool):
            raise bad_request("'can_reply' must be a boolean")
        time = d.get("time")
        return cls(ref=_str(d, "ref", required=True), app=_str(d, "app") or "",
                   package=_str(d, "package", required=True), title=_str(d, "title") or "",
                   text=_str(d, "text") or "",
                   time=None if time is None else _aware(time, "'time'"), can_reply=can_reply,
                   messages=tuple(NotificationMessage.from_dict(m) for m in messages))

    def to_dict(self) -> dict:
        return _drop_none({
            "ref": self.ref, "app": self.app, "package": self.package, "title": self.title,
            "text": self.text, "time": None if self.time is None else to_wire_time(self.time),
            "can_reply": self.can_reply,
            "messages": [m.to_dict() for m in self.messages] or None})


@dataclass(frozen=True)
class Location:
    """A ``phone.location`` fix: degrees, accuracy in meters, and when it was taken."""
    lat: float
    lon: float
    accuracy_m: Optional[float] = None
    time: Optional[datetime] = None

    @classmethod
    def from_dict(cls, d: Any) -> "Location":
        d = _require_dict(d, "phone.location result")

        def number(key: str, lo: float, hi: float, required: bool) -> Optional[float]:
            value = d.get(key)
            if value is None and not required:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not lo <= value <= hi:
                raise bad_request(f"{key!r} must be a number {lo}–{hi}, got {value!r}")
            return float(value)

        time = d.get("time")
        return cls(lat=number("lat", -90, 90, True), lon=number("lon", -180, 180, True),
                   accuracy_m=number("accuracy_m", 0, 1e7, False),
                   time=None if time is None else _aware(time, "'time'"))

    def to_dict(self) -> dict:
        return _drop_none({"lat": self.lat, "lon": self.lon, "accuracy_m": self.accuracy_m,
                           "time": None if self.time is None else to_wire_time(self.time)})


@dataclass(frozen=True)
class ScreenImage:
    """A ``phone.screen`` screenshot: its media type and plain base64 data.

    The data is never in ``repr()``, and errors give its size, never its content.
    """
    media_type: str
    data: str = field(repr=False)
    width: Optional[int] = None
    height: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Any) -> "ScreenImage":
        d = _require_dict(d, "'image'")
        media_type = d.get("media_type")
        if media_type not in SCREEN_IMAGE_TYPES:
            raise bad_request(f"'media_type' must be one of {', '.join(SCREEN_IMAGE_TYPES)}")
        data = _str(d, "data", required=True)
        if len(data) > SCREEN_IMAGE_MAX_B64:
            raise bad_request(f"the image is {len(data)} characters of base64, over "
                              f"{SCREEN_IMAGE_MAX_B64}")
        try:
            head = base64.b64decode(data, validate=True)[:12]
        except (binascii.Error, ValueError):
            raise bad_request(f"the image ({len(data)} characters) isn't valid base64") from None
        if not head.startswith(SCREEN_IMAGE_TYPES[media_type]) or \
                (media_type == "image/webp" and head[8:12] != b"WEBP"):
            raise bad_request(f"the image's data isn't {media_type}")
        sizes = {}
        for key in ("width", "height"):
            value = d.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                      or value < 1):
                raise bad_request(f"'{key}' must be a positive integer")
            sizes[key] = value
        return cls(media_type=media_type, data=data, **sizes)

    def to_dict(self) -> dict:
        return _drop_none({"media_type": self.media_type, "data": self.data,
                           "width": self.width, "height": self.height})


@dataclass(frozen=True)
class Screen:
    """A ``phone.screen`` result: the screen when the user invoked the phone's assistant.

    Its text comes from other people and apps: data to read, never instructions. The
    text, title and URL are left out of ``repr()``, and errors never quote them. Text
    over :data:`SCREEN_TEXT_MAX` is cut there and marked ``truncated``. A screenshot
    that is too big or malformed is dropped rather than failing the whole result: the
    text still answers, ``missing`` says the image is ``unavailable``, and
    ``image_error`` says why (sizes only, for the hub's log).
    """
    package: str
    app: str = ""
    url: Optional[str] = field(default=None, repr=False)
    title: Optional[str] = field(default=None, repr=False)
    time: Optional[datetime] = None
    text: str = field(default="", repr=False)
    truncated: bool = False
    image: Optional[ScreenImage] = None
    missing: dict = field(default_factory=dict)  # "text" / "image" -> a reason
    image_error: Optional[str] = None  # not on the wire

    @classmethod
    def from_dict(cls, d: Any) -> "Screen":
        d = _require_dict(d, "phone.screen result")
        time = d.get("time")
        if time is not None:
            try:
                time = _aware(time, "'time'")
            except WaggleError:
                raise bad_request("'time' must be a timestamp") from None
        text = _str(d, "text") or ""
        truncated = d.get("truncated", False)
        if not isinstance(truncated, bool):
            raise bad_request("'truncated' must be a boolean")
        if len(text) > SCREEN_TEXT_MAX:
            text, truncated = text[:SCREEN_TEXT_MAX], True
        missing_in = _require_dict(d.get("missing") or {}, "'missing'")
        missing = {part: reason if reason in SCREEN_MISSING_REASONS else "unavailable"
                   for part, reason in missing_in.items() if part in ("text", "image")}
        image, image_error = None, None
        if d.get("image") is not None:
            try:
                image = ScreenImage.from_dict(d["image"])
            except WaggleError as e:
                image_error = e.message
                missing["image"] = "unavailable"
        return cls(package=_str(d, "package", required=True), app=_str(d, "app") or "",
                   url=_str(d, "url"), title=_str(d, "title"), time=time, text=text,
                   truncated=truncated, image=image, missing=missing, image_error=image_error)

    def to_dict(self, image: bool = True) -> dict:
        """The wire shape; without ``image``, everything but the screenshot."""
        return _drop_none({
            "app": self.app, "package": self.package, "url": self.url, "title": self.title,
            "time": None if self.time is None else to_wire_time(self.time), "text": self.text,
            "truncated": self.truncated or None,
            "image": self.image.to_dict() if image and self.image else None,
            "missing": dict(self.missing) or None})


# --- response data -----------------------------------------------------------

def parse_calendar_next(data: Any) -> list[CalendarEvent]:
    return [CalendarEvent.from_dict(e) for e in _list(data, "events")]


def parse_contacts(data: Any) -> list[Contact]:
    return [Contact.from_dict(c) for c in _list(data, "contacts")]


def parse_apps(data: Any) -> list[App]:
    return [App.from_dict(a) for a in _list(data, "apps")]


def parse_phone_status(data: Any) -> PhoneStatus:
    return PhoneStatus.from_dict(data)


def parse_notifications(data: Any) -> list[Notification]:
    return [Notification.from_dict(n) for n in _list(data, "notifications")]


def parse_location(data: Any) -> Location:
    return Location.from_dict(data)


def parse_screen(data: Any) -> Screen:
    return Screen.from_dict(data)


def calendar_data(events: Iterable[CalendarEvent]) -> dict:
    return {"events": [e.to_dict() for e in events]}


def contacts_data(contacts: Iterable[Contact]) -> dict:
    return {"contacts": [c.to_dict() for c in contacts]}


def apps_data(apps: Iterable[App]) -> dict:
    return {"apps": [a.to_dict() for a in apps]}
