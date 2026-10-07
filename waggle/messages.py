"""Waggle v1 message models and validation (see WAGGLE.md).

Each model parses the ``data`` of one Waggle bus message with ``from_dict``
and produces it with ``to_dict``. Parsing ignores unknown fields and raises
:class:`WaggleError` with a WAGGLE.md error code for anything malformed.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})

CAPABILITIES = "waggle.capabilities"
INTENT = "waggle.intent"
INTENT_RESPONSE = "waggle.intent.response"
QUERY = "waggle.query"
QUERY_RESPONSE = "waggle.query.response"

MESSAGE_TYPES = frozenset({CAPABILITIES, INTENT, INTENT_RESPONSE, QUERY, QUERY_RESPONSE})
RESPONSE_TYPES = {INTENT: INTENT_RESPONSE, QUERY: QUERY_RESPONSE}


class ErrorCode(str, Enum):
    BLOCKED = "blocked"
    DECLINED = "declined"
    TIMEOUT = "timeout"
    NO_HANDLER = "no_handler"
    LAUNCH_FAILED = "launch_failed"
    PERMISSION_DENIED = "permission_denied"
    BAD_REQUEST = "bad_request"
    UNSUPPORTED_VERSION = "unsupported_version"


class Mode(str, Enum):
    """A rule's mode, or the phone's ``unmatched`` mode."""
    RUN = "run"
    ASK = "ask"
    BLOCK = "block"

    @property
    def strictness(self) -> int:
        return _STRICTNESS[self]


_STRICTNESS = {Mode.RUN: 0, Mode.ASK: 1, Mode.BLOCK: 2}

EXTRA_TYPES = frozenset({"int", "long", "float", "double", "bool", "string", "string[]", "uri"})
FORBIDDEN_SCHEMES = frozenset({"content", "file", "intent", "android-app"})

CALENDAR_NEXT = "calendar.next"
CONTACTS_LOOKUP = "contacts.lookup"
APPS_LIST = "apps.list"
QUERY_NAMES = frozenset({CALENDAR_NEXT, CONTACTS_LOOKUP, APPS_LIST})

INT_MIN, INT_MAX = -2**31, 2**31 - 1
LONG_MIN, LONG_MAX = -2**63, 2**63 - 1

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")  # RFC 3986, as Android's Uri reads it


class WaggleError(ValueError):
    """A protocol violation, carrying the WAGGLE.md error code to answer with."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message


def bad_request(message: str) -> WaggleError:
    return WaggleError(ErrorCode.BAD_REQUEST, message)


def new_request_id() -> str:
    return str(uuid.uuid4())


def uri_scheme(uri: str) -> Optional[str]:
    """The URI's scheme, lowercased (schemes are case-insensitive), or None."""
    m = _SCHEME_RE.match(uri)
    return m.group(1).lower() if m else None


def check_uri(uri: Any, what: str) -> str:
    if not isinstance(uri, str) or not uri:
        raise bad_request(f"{what} must be a non-empty string")
    scheme = uri_scheme(uri)
    if scheme is None:
        raise bad_request(f"{what} has no scheme: {uri!r}")
    if scheme in FORBIDDEN_SCHEMES:
        raise bad_request(f"{what} uses forbidden scheme {scheme!r}")
    return uri


def to_wire_time(value: datetime | date) -> str:
    """ISO 8601 UTC with a ``Z`` suffix, or ``YYYY-MM-DD`` for an all-day date."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime; Waggle times must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    return value.isoformat()


def from_wire_time(value: str) -> datetime | date:
    """Parse a Waggle timestamp: an aware UTC datetime, or a date for all-day values."""
    if not isinstance(value, str):
        raise bad_request(f"timestamp must be a string: {value!r}")
    try:
        if "T" not in value:
            if not _DATE_RE.fullmatch(value):
                raise ValueError("not YYYY-MM-DD")
            return date.fromisoformat(value)
        if not value.endswith("Z"):
            raise ValueError("missing Z suffix")
        parsed = datetime.fromisoformat(value[:-1])
        if parsed.tzinfo is not None:
            raise ValueError("offset as well as Z")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise bad_request(f"bad timestamp {value!r}: {e}") from None


# --- field helpers -----------------------------------------------------------

def _require_dict(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise bad_request(f"{what} must be an object")
    return value


def _str(d: dict, key: str, required: bool = False) -> Optional[str]:
    value = d.get(key)
    if value is None:
        if required:
            raise bad_request(f"missing {key!r}")
        return None
    if not isinstance(value, str) or (required and not value):
        raise bad_request(f"{key!r} must be a non-empty string" if required else f"{key!r} must be a string")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# --- capabilities ------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    action: str
    mode: Mode
    scheme: Optional[str] = None
    package: Optional[str] = None
    category: Optional[str] = None

    def __post_init__(self):
        if self.scheme:
            object.__setattr__(self, "scheme", self.scheme.lower())

    @property
    def specificity(self) -> int:
        """How many fields the rule sets (the action always counts)."""
        return 1 + sum(v is not None for v in (self.scheme, self.package, self.category))

    @classmethod
    def from_dict(cls, d: Any) -> "Rule":
        d = _require_dict(d, "rule")
        try:
            mode = Mode(d.get("mode"))
        except ValueError:
            raise bad_request(f"bad rule mode {d.get('mode')!r}") from None
        optional = {key: _str(d, key) for key in ("scheme", "package", "category")}
        if "" in optional.values():
            raise bad_request("rule fields must be non-empty when set")
        return cls(action=_str(d, "action", required=True), mode=mode, **optional)

    def to_dict(self) -> dict:
        return _drop_none({"action": self.action, "scheme": self.scheme, "package": self.package,
                           "category": self.category, "mode": self.mode.value})


@dataclass(frozen=True)
class ClientInfo:
    name: str
    version: Optional[str] = None


@dataclass(frozen=True)
class Capabilities:
    version: int
    rules: tuple[Rule, ...] = ()
    queries: frozenset[str] = frozenset()
    unmatched: Mode = Mode.BLOCK
    timezone: Optional[str] = None
    lang: Optional[str] = None
    ask_timeout_s: Optional[float] = None
    client: Optional[ClientInfo] = None

    @classmethod
    def from_dict(cls, d: Any) -> "Capabilities":
        """Parse capabilities; raises ``unsupported_version`` for a version we don't speak."""
        d = _require_dict(d, "capabilities")
        version = d.get("version")
        if not _is_int(version):
            raise bad_request(f"bad version {version!r}")
        if version not in SUPPORTED_VERSIONS:
            raise WaggleError(ErrorCode.UNSUPPORTED_VERSION, f"unsupported Waggle version {version}")
        unmatched = d.get("unmatched", Mode.BLOCK.value)
        if unmatched not in (Mode.BLOCK.value, Mode.ASK.value):
            raise bad_request(f"bad unmatched mode {unmatched!r}")
        rules = d.get("rules", [])
        queries = d.get("queries", [])
        if not isinstance(rules, list):
            raise bad_request("'rules' must be a list")
        if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
            raise bad_request("'queries' must be a list of strings")
        ask_timeout = d.get("ask_timeout_s")
        if ask_timeout is not None and (isinstance(ask_timeout, bool)
                                        or not isinstance(ask_timeout, (int, float)) or ask_timeout < 0):
            raise bad_request(f"bad ask_timeout_s {ask_timeout!r}")
        client = d.get("client")
        if isinstance(client, dict) and isinstance(client.get("name"), str):
            client_version = client.get("version")
            client = ClientInfo(client["name"], client_version if isinstance(client_version, str) else None)
        else:
            client = None
        return cls(version=version, rules=tuple(Rule.from_dict(r) for r in rules),
                   queries=frozenset(queries), unmatched=Mode(unmatched),
                   timezone=_str(d, "timezone"), lang=_str(d, "lang"),
                   ask_timeout_s=ask_timeout, client=client)

    def to_dict(self) -> dict:
        return _drop_none({
            "version": self.version,
            "client": _drop_none({"name": self.client.name, "version": self.client.version})
            if self.client else None,
            "timezone": self.timezone,
            "lang": self.lang,
            "ask_timeout_s": self.ask_timeout_s,
            "unmatched": self.unmatched.value,
            "rules": [r.to_dict() for r in self.rules],
            "queries": sorted(self.queries),
        })


# --- intents -----------------------------------------------------------------

@dataclass(frozen=True)
class Extra:
    type: str
    value: Any

    @classmethod
    def from_dict(cls, name: str, d: Any) -> "Extra":
        d = _require_dict(d, f"extra {name!r}")
        extra = cls(d.get("type"), d.get("value"))
        extra.validate(name)
        return extra

    def validate(self, name: str = "extra") -> None:
        t, v = self.type, self.value
        if t not in EXTRA_TYPES:
            raise bad_request(f"extra {name!r} has unknown type {t!r}")
        ok = {
            "int": lambda: _is_int(v) and INT_MIN <= v <= INT_MAX,
            "long": lambda: _is_int(v) and LONG_MIN <= v <= LONG_MAX,
            "float": lambda: _is_int(v) or isinstance(v, float),
            "double": lambda: _is_int(v) or isinstance(v, float),
            "bool": lambda: isinstance(v, bool),
            "string": lambda: isinstance(v, str),
            "string[]": lambda: isinstance(v, list) and all(isinstance(s, str) for s in v),
            "uri": lambda: True,  # checked below
        }[t]()
        if not ok:
            raise bad_request(f"extra {name!r} value {v!r} is not a valid {t}")
        if t == "uri":
            check_uri(v, f"extra {name!r}")

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value}


@dataclass(frozen=True)
class Intent:
    """The data of a ``waggle.intent`` request."""
    id: str
    action: str
    description: Optional[str] = None
    data: Optional[str] = None
    mime_type: Optional[str] = None
    categories: tuple[str, ...] = ()
    package: Optional[str] = None
    extras: dict[str, Extra] = field(default_factory=dict)

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise bad_request("'id' must be a non-empty string")
        if not isinstance(self.action, str) or not self.action:
            raise bad_request("'action' must be a non-empty string")
        if self.data is not None:
            check_uri(self.data, "'data'")
        if not all(isinstance(c, str) and c for c in self.categories):
            raise bad_request("'categories' must be non-empty strings")
        for name, extra in self.extras.items():
            if not isinstance(name, str) or not name:
                raise bad_request("extra names must be non-empty strings")
            extra.validate(name)

    @property
    def scheme(self) -> Optional[str]:
        return uri_scheme(self.data) if self.data else None

    @classmethod
    def from_dict(cls, d: Any) -> "Intent":
        d = _require_dict(d, "intent")
        categories = d.get("categories", [])
        if not isinstance(categories, list):
            raise bad_request("'categories' must be a list")
        extras = _require_dict(d.get("extras", {}), "'extras'")
        return cls(id=_str(d, "id", required=True), action=_str(d, "action", required=True),
                   description=_str(d, "description"), data=_str(d, "data"),
                   mime_type=_str(d, "mime_type"), categories=tuple(categories),
                   package=_str(d, "package"),
                   extras={name: Extra.from_dict(name, e) for name, e in extras.items()})

    def to_dict(self) -> dict:
        return _drop_none({
            "id": self.id, "description": self.description, "action": self.action,
            "data": self.data, "mime_type": self.mime_type,
            "categories": list(self.categories) or None, "package": self.package,
            "extras": {k: e.to_dict() for k, e in self.extras.items()} or None,
        })


# --- queries -----------------------------------------------------------------

def _int_param(params: dict, key: str, default: int, lo: int, hi: Optional[int] = None) -> int:
    value = params.get(key, default)
    if not _is_int(value) or value < lo or (hi is not None and value > hi):
        bounds = f"{lo}–{hi}" if hi is not None else f">= {lo}"
        raise bad_request(f"param {key!r} must be an integer {bounds}, got {value!r}")
    return value


def _validate_params(name: str, params: dict) -> None:
    if name == CALENDAR_NEXT:
        _int_param(params, "count", 1, 1, 10)
        _int_param(params, "within_days", 7, 1)
    elif name == CONTACTS_LOOKUP:
        _str(params, "name", required=True)
        _int_param(params, "limit", 5, 1)
    elif name == APPS_LIST:
        _str(params, "name")
        _int_param(params, "limit", 20, 1)


@dataclass(frozen=True)
class Query:
    """The data of a ``waggle.query`` request."""
    id: str
    name: str
    params: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise bad_request("'id' must be a non-empty string")
        if self.name not in QUERY_NAMES:
            raise bad_request(f"unknown query {self.name!r}")
        _validate_params(self.name, _require_dict(self.params, "'params'"))

    @classmethod
    def from_dict(cls, d: Any) -> "Query":
        d = _require_dict(d, "query")
        return cls(id=_str(d, "id", required=True), name=_str(d, "name", required=True),
                   params={} if d.get("params") is None else d["params"])

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "params": dict(self.params)}


# --- responses ---------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    """The data of a ``waggle.intent.response`` or ``waggle.query.response``."""
    id: str
    ok: bool
    error: Optional[ErrorCode] = None
    message: Optional[str] = None
    data: Optional[dict] = None

    def __post_init__(self):
        if not isinstance(self.id, str):
            raise bad_request("'id' must be a string")
        if not isinstance(self.ok, bool):
            raise bad_request("'ok' must be a boolean")
        if self.ok == (self.error is not None):
            raise bad_request("'error' must be set if and only if 'ok' is false")

    @classmethod
    def success(cls, id: str, data: Optional[dict] = None) -> "Response":
        return cls(id=id, ok=True, data=data)

    @classmethod
    def failure(cls, id: str, error: ErrorCode | str, message: Optional[str] = None) -> "Response":
        try:
            error = ErrorCode(error)
        except ValueError:
            raise bad_request(f"unknown error code {error!r}") from None
        return cls(id=id, ok=False, error=error, message=message)

    @classmethod
    def from_dict(cls, d: Any) -> "Response":
        d = _require_dict(d, "response")
        error = d.get("error")
        if error is not None:
            try:
                error = ErrorCode(error)
            except ValueError:
                # Newer phones may add codes (WAGGLE.md "Versioning"); keep the raw string.
                if not isinstance(error, str):
                    raise bad_request(f"bad error code {error!r}") from None
        data = d.get("data")
        if data is not None:
            _require_dict(data, "'data'")
        # An empty id is legal here: it answers a request that had no usable id.
        if not isinstance(d.get("id"), str):
            raise bad_request("'id' must be a string")
        return cls(id=d["id"], ok=d.get("ok"), error=error,
                   message=_str(d, "message"), data=data)

    def to_dict(self) -> dict:
        error = self.error.value if isinstance(self.error, ErrorCode) else self.error
        return _drop_none({"id": self.id, "ok": self.ok, "error": error,
                           "message": self.message, "data": self.data})
