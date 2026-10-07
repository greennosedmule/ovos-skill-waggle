"""Each phone's app names, so the stage can tell "open spotify" from "open the garage door".

"Open X" without the word "app" is only Waggle's when the phone has an app
called X; otherwise it goes on to the hub's skills (Home Assistant's "open
the garage door"). The stage must decide at once, so it can't ask the phone
then. Instead, when a phone that shares ``apps.list`` announces its
capabilities, the catalog fetches the phone's app labels in the background
and keeps them by HiveMind client, refreshing them at most every
``refresh_s``. A match waits briefly for a fetch already under way.

The handler always looks the app up again when it runs, so the catalog only
gates matching.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle import queries
from waggle.client import WaggleClient
from waggle.messages import APPS_LIST
from waggle.rules import query_enabled

from ovos_skill_waggle.capabilities import Phone

# How many apps to fetch: every launcher app on a typical phone.
FETCH_LIMIT = 500
REFRESH_S = 30 * 60
WAIT_FOR_FETCH_S = 1.5


def fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def names_app(name: str, labels: list[str]) -> bool:
    """Whether ``name`` (as said) names one of ``labels``: "maps" for "Maps", "camera" for
    "Camera", "google maps" for "Maps", "spotify" for "Spotify: Music and Podcasts"."""
    said = fold(name)
    if not said:
        return False
    for label in labels:
        label = fold(label)
        if not label:
            continue
        if (label == said or re.search(rf"\b{re.escape(said)}\b", label)
                or re.search(rf"\b{re.escape(label)}\b", said)):
            return True
    return False


@dataclass
class _Entry:
    labels: Optional[list[str]] = None
    fetched_at: float = 0.0
    fetching: Optional[threading.Event] = None


class AppCatalog:
    """App labels by HiveMind client id, fetched from each phone in the background."""

    def __init__(self, bus, timeout_s: Callable[[], float], refresh_s: float = REFRESH_S,
                 clock: Callable[[], float] = time.monotonic):
        self.client = WaggleClient(bus)
        self.timeout_s = timeout_s
        self.refresh_s = refresh_s
        self.clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def refresh(self, phone: Phone, origin: Message, wait: bool = False) -> None:
        """Fetch ``phone``'s app labels if it shares them and ours are stale.

        ``origin`` is a message addressed as from the phone (its reply reaches
        the phone). ``wait`` runs the fetch in this thread (tests).
        """
        if not query_enabled(phone.capabilities, APPS_LIST):
            with self._lock:
                self._entries.pop(phone.client_id, None)
            return
        with self._lock:
            entry = self._entries.setdefault(phone.client_id, _Entry())
            if entry.fetching is not None:
                return
            if entry.labels is not None and self.clock() - entry.fetched_at < self.refresh_s:
                return
            entry.fetching = threading.Event()
        if wait:
            self._fetch(phone, origin, entry)
        else:
            threading.Thread(target=self._fetch, args=(phone, origin, entry), daemon=True,
                             name=f"waggle-apps-{phone.client_id}").start()

    def _fetch(self, phone: Phone, origin: Message, entry: _Entry) -> None:
        labels = None
        try:
            response = self.client.send_query(origin, queries.apps_list(None, FETCH_LIMIT),
                                              self.timeout_s())
            if response is not None and response.ok:
                labels = [app.label for app in queries.parse_apps(response.data or {})]
            else:
                LOG.info(f"Waggle: no app list from {phone.peer}: "
                         f"{response.error if response else 'no answer'}")
        except Exception as e:  # a bad answer must not kill the thread
            LOG.warning(f"Waggle: fetching the app list from {phone.peer} failed: {e}")
        with self._lock:
            done, entry.fetching = entry.fetching, None
            if labels is not None:
                entry.labels, entry.fetched_at = labels, self.clock()
        if done is not None:
            done.set()

    def has_app(self, client_id: str, name: str, wait_s: float = WAIT_FOR_FETCH_S) -> bool:
        """Whether the phone has an app ``name`` names, waiting up to ``wait_s`` for a fetch."""
        with self._lock:
            entry = self._entries.get(client_id)
            fetching = entry.fetching if entry else None
        if fetching is not None and entry.labels is None:
            fetching.wait(wait_s)
        with self._lock:
            labels = entry.labels if entry else None
        return labels is not None and names_app(name, labels)

    def labels(self, client_id: str) -> Optional[list[str]]:
        with self._lock:
            entry = self._entries.get(client_id)
            return list(entry.labels) if entry and entry.labels is not None else None
