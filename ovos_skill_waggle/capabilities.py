"""The capability cache: each phone's latest ``waggle.capabilities`` (see SPEC.md "Request/response flow").

HiveMind tags every message from a client with its peer id,
``<useragent>::<client id>::<client name>::<session id>``, in both
``context["peer"]`` and ``context["source"]`` (hivemind-core
``handle_inject_agent_msg``; the name comes from hivemind-websocket-protocol).
The session id changes with every HiveMind session, so the cache is keyed by
the client id, which ``hivemind-core add-client`` assigns once, and keeps the
latest full peer for addressing the phone.

A peer that isn't in HiveMind's form (a local test client, say) is its own key.

The cache is kept in a JSON file, so a hub restart doesn't forget the phones
(a phone that stays connected doesn't announce again until its rules change).
A phone's entry is rewritten whenever its capabilities change; a new peer
alone isn't written, since replies are addressed by each request's own peer.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.messages import Capabilities, ErrorCode, WaggleError
from waggle.rules import limited

PEER_SEPARATOR = "::"
# The cache file's format, in case it changes.
FILE_VERSION = 1


def peer_of(message: Message) -> Optional[str]:
    """The HiveMind peer a message came from: ``context["peer"]``, else ``context["source"]``.

    Replies swap ``source`` and ``destination`` but keep ``peer``, so this also
    works on the match message ovos-core emits as a reply to the utterance.
    """
    context = message.context or {}
    for key in ("peer", "source"):
        value = context.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def client_id_of(peer: str) -> str:
    """The stable part of a peer id: its client id, or the whole peer if it has none."""
    parts = peer.split(PEER_SEPARATOR)
    if len(parts) >= 4 and parts[1]:
        return parts[1]
    return peer


@dataclass(frozen=True)
class Phone:
    """A phone that announced capabilities this package supports."""
    client_id: str
    peer: str  # the latest full peer id it announced from
    capabilities: Capabilities
    updated: float  # time.time() of the announcement

    @property
    def tz(self) -> Optional[tzinfo]:
        """The phone's timezone, or None if it announced none or one we don't know."""
        name = self.capabilities.timezone
        if not name:
            return None
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            LOG.warning(f"Waggle: unknown timezone {name!r} from {self.client_id}")
            return None


# Context keys a phone may set on an utterance (WAGGLE.md "Common rules"). Only false counts:
# absent, the phone's announced rules apply as they are.
CAN_ASK = "can_ask"            # false: it can't ask its user now, so an "ask" rule blocks
PHONE_ACTIONS = "phone_actions"  # false: it takes no requests now
WAGGLE_OFF = "waggle_off"      # the queries and commands it won't run now


def _off(context: dict) -> frozenset[str]:
    """The names in ``waggle_off``; anything but a list of strings counts as none."""
    value = context.get(WAGGLE_OFF)
    if not isinstance(value, list):
        return frozenset()
    return frozenset(name for name in value if isinstance(name, str))


def as_heard(phone: Phone, message: Optional[Message]) -> Phone:
    """``phone`` with its rules as they apply to ``message``'s utterance: the same object
    unless the phone said it can't ask, takes no requests, or won't run some queries or
    commands, right now."""
    context = message.context if message is not None and isinstance(message.context, dict) else {}
    can_ask = context.get(CAN_ASK) is not False
    actions = context.get(PHONE_ACTIONS) is not False
    off = _off(context)
    if can_ask and actions and not off & (phone.capabilities.queries
                                          | phone.capabilities.commands):
        return phone
    return replace(phone, capabilities=limited(phone.capabilities, can_ask=can_ask,
                                               actions=actions, off=off))


class CapabilityCache:
    """Thread-safe map from HiveMind client id to its latest announcement.

    An announcement in a version this package doesn't speak replaces any
    earlier one with a rejection, so the phone is no longer treated as a
    Waggle phone; :meth:`rejected_version` says why.

    With a ``path``, the phones are loaded from that file and saved to it
    whenever one is added, changes its capabilities or is dropped. Rejections
    aren't saved. A file that can't be read or written is logged and otherwise
    ignored: the cache then works from memory, as it would without one.
    """

    def __init__(self, path: Union[str, Path, None] = None):
        self._lock = threading.Lock()
        self._phones: dict[str, Phone] = {}
        self._rejected: dict[str, object] = {}
        self.path = Path(path) if path else None
        if self.path is not None:
            self._load()

    def update(self, message: Message) -> Optional[Phone]:
        """Record a ``waggle.capabilities`` message; the phone, or None if rejected."""
        peer = peer_of(message)
        if peer is None:
            LOG.warning("Waggle: ignoring capabilities without a peer or source")
            return None
        client_id = client_id_of(peer)
        try:
            capabilities = Capabilities.from_dict(message.data)
        except WaggleError as e:
            with self._lock:
                dropped = self._phones.pop(client_id, None)
                if e.code is ErrorCode.UNSUPPORTED_VERSION:
                    self._rejected[client_id] = message.data["version"]
                else:
                    self._rejected.pop(client_id, None)
                if dropped is not None:
                    self._save()
            LOG.warning(f"Waggle: rejected capabilities from {peer}: {e.message}")
            return None
        phone = Phone(client_id, peer, capabilities, time.time())
        with self._lock:
            before = self._phones.get(client_id)
            self._phones[client_id] = phone
            self._rejected.pop(client_id, None)
            if before is None or before.capabilities.to_dict() != capabilities.to_dict():
                self._save()
        LOG.info(f"Waggle: capabilities from {peer} (v{capabilities.version}, "
                 f"{len(capabilities.rules)} rules, tz {capabilities.timezone})")
        return phone

    def get(self, key: Union[Message, str, None]) -> Optional[Phone]:
        """The phone for a message (by its peer), a peer id or a client id."""
        if isinstance(key, Message):
            key = peer_of(key)
        if not key:
            return None
        with self._lock:
            return self._phones.get(client_id_of(key))

    def rejected_version(self, key: Union[Message, str, None]) -> Optional[object]:
        """The unsupported version a peer last announced, or None."""
        if isinstance(key, Message):
            key = peer_of(key)
        if not key:
            return None
        with self._lock:
            return self._rejected.get(client_id_of(key))

    def forget(self, key: str) -> None:
        with self._lock:
            dropped = self._phones.pop(client_id_of(key), None)
            self._rejected.pop(client_id_of(key), None)
            if dropped is not None:
                self._save()

    def phones(self) -> list[Phone]:
        with self._lock:
            return list(self._phones.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._phones)

    # --- the file --------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            LOG.warning(f"Waggle: can't read {self.path}, starting with no phones: {e}")
            return
        if not isinstance(saved, dict) or saved.get("version") != FILE_VERSION:
            LOG.warning(f"Waggle: ignoring {self.path}: not a version {FILE_VERSION} cache")
            return
        phones = saved.get("phones")
        for client_id, entry in (phones if isinstance(phones, dict) else {}).items():
            try:
                phone = Phone(client_id, entry["peer"],
                              Capabilities.from_dict(entry["capabilities"]),
                              float(entry["updated"]))
            except (KeyError, TypeError, ValueError, WaggleError) as e:
                LOG.warning(f"Waggle: dropping the saved capabilities of {client_id}: {e}")
                continue
            self._phones[client_id] = phone
        LOG.info(f"Waggle: {len(self._phones)} phones from {self.path}")

    def _save(self) -> None:
        """Write every phone to the file. Called with the lock held."""
        if self.path is None:
            return
        saved = {"version": FILE_VERSION, "phones": {
            phone.client_id: {"peer": phone.peer, "capabilities": phone.capabilities.to_dict(),
                              "updated": phone.updated}
            for phone in self._phones.values()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A new file renamed over the old one, so a crash never leaves half a file.
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(saved, f, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as e:
            LOG.warning(f"Waggle: can't save the phones to {self.path}: {e}")
