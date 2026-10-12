"""Each session's utterances as the phone sent them, for text messages (see SPEC.md "The pipeline stage").

ovos-core's utterance normalizer rewrites every utterance before the
pipeline sees it, and strips leading and trailing punctuation even from the
original it passes on after its rewrite, so a text message would lose its
final "!" or "?". The phone's own ``recognizer_loop:utterance`` still has
them: the bus delivers it to listeners on that topic as its own copy, before
any transformer runs. :class:`SaidUtterances` keeps the last few of those per
session, and :meth:`~SaidUtterances.as_said` finds the one a transformed
utterance came from.

The bus may run this listener and ovos-core's at the same time, so a lookup
can wait briefly for the utterance to be recorded.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, deque
from difflib import SequenceMatcher
from typing import Callable, Optional

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager

# How long an utterance is kept, and for how many sessions.
KEEP_S = 120.0
MAX_SESSIONS = 256
PER_SESSION = 4
# How alike a transformed utterance and the one as said must read (see parsing.original_wording).
MIN_RATIO = 0.8


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def reads_as(heard: str, said: str) -> bool:
    """Whether ``said`` is ``heard`` before normalizing: nearly the same letters and digits."""
    loose = [_loose(heard), _loose(said)]
    return bool(loose[1]) and SequenceMatcher(None, *loose).ratio() >= MIN_RATIO


class SaidUtterances:
    """The latest utterances of each session, as sent. Thread-safe."""

    def __init__(self, keep_s: float = KEEP_S, clock: Callable[[], float] = time.monotonic):
        self.keep_s = keep_s
        self.clock = clock
        self._sessions: OrderedDict[str, deque] = OrderedDict()
        self._changed = threading.Condition()

    def record(self, message: Message) -> None:
        """Keep a ``recognizer_loop:utterance``'s utterances under its session."""
        data = message.data if isinstance(message.data, dict) else {}
        utterances = [u for u in data.get("utterances") or [] if isinstance(u, str) and u.strip()]
        if not utterances:
            return
        session_id = SessionManager.get(message).session_id
        with self._changed:
            said = self._sessions.pop(session_id, None) or deque(maxlen=PER_SESSION)
            said.append((self.clock(), utterances))
            self._sessions[session_id] = said
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            self._changed.notify_all()

    def as_said(self, session_id: str, heard: str, wait_s: float = 0.0) -> Optional[str]:
        """The utterance ``heard`` was transformed from, newest first, or None.

        Waits up to ``wait_s`` for it to be recorded.
        """
        deadline = self.clock() + wait_s
        with self._changed:
            while True:
                found = self._find(session_id, heard)
                remaining = deadline - self.clock()
                if found is not None or remaining <= 0:
                    return found
                self._changed.wait(remaining)

    def _find(self, session_id: str, heard: str) -> Optional[str]:
        now = self.clock()
        for at, utterances in reversed(self._sessions.get(session_id) or ()):
            if now - at > self.keep_s:
                break
            for said in utterances:
                if reads_as(heard, said):
                    return said
        return None
