"""Shared fixtures for the ovos_skill_waggle tests.

The plugin is a real ovos-workshop application, which writes a settings file
under the XDG config directory on start. The XDG directories are pointed at a
throwaway directory before anything from OVOS is imported, so the tests never
touch the user's own config.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_XDG = tempfile.mkdtemp(prefix="waggle-tests-")
atexit.register(shutil.rmtree, _XDG, ignore_errors=True)
for _name in ("CONFIG", "DATA", "CACHE", "STATE"):
    os.environ[f"XDG_{_name}_HOME"] = os.path.join(_XDG, _name.lower())

from datetime import datetime, timezone  # noqa: E402
from typing import Optional  # noqa: E402

import pytest  # noqa: E402
from ovos_bus_client.message import Message  # noqa: E402
from ovos_utils.fakebus import FakeBus  # noqa: E402

from ovos_skill_waggle.pipeline import WagglePipeline  # noqa: E402
from waggle.fake_phone import DEFAULT_CAPABILITIES, FakePhone  # noqa: E402
from waggle.messages import Capabilities  # noqa: E402

# A HiveMind peer id: <useragent>::<client id>::<client name>::<session id>.
PEER = "Wiggins/0.3.0::7::pixel-8::session-1"
OTHER_PEER = "HiveMind-voice-sat::3::kitchen-pi::session-9"
# 2026-10-07 19:10 UTC = 14:10 in Chicago (the fake phone's zone).
NOW = datetime(2026, 10, 7, 19, 10, tzinfo=timezone.utc)

# Fast timeouts, so timeout tests take tenths of a second.
FAST = {"response_timeout_s": 0.3, "ask_margin_s": 0.2}


def capabilities(**overrides) -> Capabilities:
    """The fake phone's default capabilities with some fields replaced."""
    return Capabilities.from_dict({**DEFAULT_CAPABILITIES.to_dict(), **overrides})


def utterance(text: str, peer: Optional[str] = PEER, lang: str = "en-US") -> Message:
    """A ``recognizer_loop:utterance`` as hivemind-core injects it from ``peer``."""
    context = {"source": peer, "peer": peer, "destination": "skills"} if peer else {}
    return Message("recognizer_loop:utterance", {"utterances": [text], "lang": lang}, context)


def match_message(utt: Message, match) -> Message:
    """What ovos-core's IntentService emits for a match (``_emit_match_message``)."""
    data = {**utt.data, **match.match_data, "utterance": match.utterance, "lang": "en-US"}
    reply = utt.reply(match.match_type, data)
    reply.context["skill_id"] = match.skill_id
    return reply


class Recorder:
    """Every message on the bus, in order."""

    def __init__(self, bus):
        self.messages: list[Message] = []
        bus.on("message", self._on_message)

    def _on_message(self, raw: str) -> None:
        self.messages.append(Message.deserialize(raw))

    def of(self, msg_type: str) -> list[Message]:
        return [m for m in self.messages if m.msg_type == msg_type]

    def types(self) -> list[str]:
        return [m.msg_type for m in self.messages]

    def clear(self) -> None:
        self.messages.clear()


@pytest.fixture
def bus():
    return FakeBus()


@pytest.fixture
def recorder(bus):
    return Recorder(bus)


class TestPipeline(WagglePipeline):
    __test__ = False  # not a test class, despite the name

    def __del__(self):
        # ovos-workshop's __del__ shuts the app down again, often at interpreter
        # exit where it logs errors; the fixture has already shut it down.
        pass


@pytest.fixture
def make_pipeline(bus):
    made = []

    def make(config: Optional[dict] = None, now: datetime = NOW):
        pipeline = TestPipeline(bus, dict(FAST if config is None else config))
        pipeline.clock = lambda: now
        made.append(pipeline)
        return pipeline

    yield make
    for pipeline in made:
        pipeline.default_shutdown()


@pytest.fixture
def pipeline(make_pipeline):
    return make_pipeline()


@pytest.fixture
def make_phone(bus):
    made = []

    def make(peer: str = PEER, announce: bool = True, **kwargs) -> FakePhone:
        phone = FakePhone(bus, peer=peer, **kwargs)
        if announce:
            phone.announce()
        made.append(phone)
        return phone

    yield make
    for phone in made:
        phone.close()


@pytest.fixture
def phone(pipeline, make_phone):
    """A phone that announced to the default pipeline (which must exist first to hear it)."""
    return make_phone()
