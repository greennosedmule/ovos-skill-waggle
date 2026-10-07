"""The capability cache: each phone's latest ``waggle.capabilities`` (see SPEC.md "Request/response flow").

HiveMind tags every message from a client with its peer id,
``<useragent>::<client id>::<client name>::<session id>``, in both
``context["peer"]`` and ``context["source"]`` (hivemind-core
``handle_inject_agent_msg``; the name comes from hivemind-websocket-protocol).
The session id changes with every HiveMind session, so the cache is keyed by
the client id, which ``hivemind-core add-client`` assigns once, and keeps the
latest full peer for addressing the phone later (S4's ``default_phone_peer``).

A peer that isn't in HiveMind's form (a local test client, say) is its own key.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import tzinfo
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.messages import Capabilities, ErrorCode, WaggleError

PEER_SEPARATOR = "::"


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


class CapabilityCache:
    """Thread-safe map from HiveMind client id to its latest announcement.

    An announcement in a version this package doesn't speak replaces any
    earlier one with a rejection, so the phone is no longer treated as a
    Waggle phone; :meth:`rejected_version` says why.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._phones: dict[str, Phone] = {}
        self._rejected: dict[str, object] = {}

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
                self._phones.pop(client_id, None)
                if e.code is ErrorCode.UNSUPPORTED_VERSION:
                    self._rejected[client_id] = message.data["version"]
                else:
                    self._rejected.pop(client_id, None)
            LOG.warning(f"Waggle: rejected capabilities from {peer}: {e.message}")
            return None
        phone = Phone(client_id, peer, capabilities, time.time())
        with self._lock:
            self._phones[client_id] = phone
            self._rejected.pop(client_id, None)
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
            self._phones.pop(client_id_of(key), None)
            self._rejected.pop(client_id_of(key), None)

    def phones(self) -> list[Phone]:
        with self._lock:
            return list(self._phones.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._phones)
