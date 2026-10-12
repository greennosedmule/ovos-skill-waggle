"""The capability cache: peer parsing, keying by client id across sessions, rejections, the file."""
from __future__ import annotations

import json
import threading

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.capabilities import CapabilityCache, client_id_of, peer_of
from waggle.fake_phone import DEFAULT_CAPABILITIES
from waggle.messages import CAPABILITIES

from conftest import PEER, capabilities, utterance


def announcement(peer, data=None, **context):
    context = {"source": peer, "peer": peer, "destination": "skills", **context}
    return Message(CAPABILITIES, DEFAULT_CAPABILITIES.to_dict() if data is None else data, context)


@pytest.mark.parametrize("peer,client_id", [
    ("Wiggins/0.3.0::7::pixel-8::session-1", "7"),
    ("Wiggins::7::pixel::other-session", "7"),
    ("HiveMind-voice-sat::abc-123::kitchen::s", "abc-123"),
    ("ua::7::name::with::colons::sess", "7"),
    ("fake-phone", "fake-phone"),           # not HiveMind's form: its own key
    ("ua::::name::sess", "ua::::name::sess"),  # no client id
    ("only::two", "only::two"),
])
def test_client_id_of(peer, client_id):
    assert client_id_of(peer) == client_id


def test_peer_of_prefers_peer_then_source():
    assert peer_of(Message("x", {}, {"peer": "a", "source": "b"})) == "a"
    assert peer_of(Message("x", {}, {"source": "b"})) == "b"
    assert peer_of(Message("x", {}, {})) is None
    assert peer_of(Message("x", {}, {"peer": "", "source": 5})) is None


def test_peer_of_survives_reply():
    # ovos-core's match message is a reply: source and destination swap, peer stays.
    utt = Message("recognizer_loop:utterance", {}, {"source": PEER, "peer": PEER,
                                                    "destination": "skills"})
    reply = utt.reply("waggle:utterance", {})
    assert reply.context["source"] == "skills"
    assert peer_of(reply) == PEER


def test_update_and_get():
    cache = CapabilityCache()
    phone = cache.update(announcement(PEER))
    assert phone.client_id == "7" and phone.peer == PEER
    assert phone.capabilities == DEFAULT_CAPABILITIES
    assert cache.get(PEER) is phone
    assert cache.get("7") is phone
    assert cache.get(announcement(PEER)) is phone
    assert len(cache) == 1 and cache.phones() == [phone]


def test_keyed_by_client_id_across_sessions():
    cache = CapabilityCache()
    cache.update(announcement("Wiggins::7::pixel::session-1"))
    # The phone's next HiveMind session has a new session id; the client id is the same.
    later = "Wiggins::7::pixel::session-2"
    assert cache.get(later).capabilities == DEFAULT_CAPABILITIES
    # A new announcement replaces the entry and records the latest peer.
    newer = capabilities(ask_timeout_s=30)
    cache.update(announcement(later, newer.to_dict()))
    assert len(cache) == 1
    assert cache.get("Wiggins::7::pixel::session-1").peer == later
    assert cache.get("7").capabilities.ask_timeout_s == 30


def test_clients_are_separate():
    cache = CapabilityCache()
    cache.update(announcement("Wiggins::7::pixel::s"))
    cache.update(announcement("Wiggins::8::tablet::s", capabilities(timezone="Asia/Tokyo").to_dict()))
    assert cache.get("7").capabilities.timezone == "America/Chicago"
    assert cache.get("8").capabilities.timezone == "Asia/Tokyo"
    assert cache.get("Wiggins::9::other::s") is None


def test_unsupported_version_rejected_and_replaces_entry():
    cache = CapabilityCache()
    cache.update(announcement(PEER))
    assert cache.update(announcement(PEER, {**DEFAULT_CAPABILITIES.to_dict(), "version": 2})) is None
    assert cache.get(PEER) is None
    assert cache.rejected_version(PEER) == 2
    # A supported announcement clears the rejection.
    cache.update(announcement(PEER))
    assert cache.get(PEER) is not None and cache.rejected_version(PEER) is None


@pytest.mark.parametrize("data", [
    {"version": "1"},
    {"version": 1, "unmatched": "run"},
    {"version": 1, "rules": [{"action": "x", "mode": "maybe"}]},
    {"version": 1, "rules": "none"},
    {},
])
def test_malformed_capabilities_rejected(data):
    cache = CapabilityCache()
    cache.update(announcement(PEER))
    assert cache.update(announcement(PEER, data)) is None
    assert cache.get(PEER) is None
    assert cache.rejected_version(PEER) is None


def test_no_peer_ignored():
    cache = CapabilityCache()
    assert cache.update(Message(CAPABILITIES, DEFAULT_CAPABILITIES.to_dict(), {})) is None
    assert len(cache) == 0
    assert cache.get(None) is None and cache.get("") is None


def test_forget():
    cache = CapabilityCache()
    cache.update(announcement(PEER))
    cache.forget("Wiggins::7::x::new-session")
    assert cache.get(PEER) is None


def test_timezone():
    cache = CapabilityCache()
    assert str(cache.update(announcement(PEER)).tz) == "America/Chicago"
    assert cache.update(announcement(PEER, capabilities(timezone="Mars/Olympus").to_dict())).tz is None
    data = DEFAULT_CAPABILITIES.to_dict()
    del data["timezone"]
    assert cache.update(announcement(PEER, data)).tz is None


def test_concurrent_updates():
    cache = CapabilityCache()

    def announce(i):
        for _ in range(50):
            cache.update(announcement(f"Wiggins::{i % 4}::p::s{i}"))
            cache.get(f"{i % 4}")

    threads = [threading.Thread(target=announce, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cache) == 4


def test_pipeline_caches_announcements(pipeline, make_phone):
    make_phone(peer="Wiggins::7::pixel::s1")
    assert pipeline.capabilities.get("7") is not None
    make_phone(peer="Wiggins::9::tablet::s1", capabilities=capabilities(timezone="Europe/Paris"))
    assert pipeline.capabilities.get("9").capabilities.timezone == "Europe/Paris"


# --- the file ----------------------------------------------------------------

def test_saved_and_loaded(tmp_path):
    path = tmp_path / "phones" / "capabilities.json"
    cache = CapabilityCache(path)
    cache.update(announcement("Wiggins::7::pixel::s1"))
    cache.update(announcement("Wiggins::8::tablet::s1", capabilities(timezone="Asia/Tokyo").to_dict()))
    assert path.exists()

    restarted = CapabilityCache(path)
    assert len(restarted) == 2
    assert restarted.get("Wiggins::7::pixel::s2").capabilities == DEFAULT_CAPABILITIES
    assert restarted.get("8").capabilities.timezone == "Asia/Tokyo"
    assert restarted.get("8").peer == "Wiggins::8::tablet::s1"


def test_written_only_when_capabilities_change(tmp_path, monkeypatch):
    cache = CapabilityCache(tmp_path / "c.json")
    writes = []
    save = cache._save
    monkeypatch.setattr(cache, "_save", lambda: (writes.append(1), save()))
    cache.update(announcement("Wiggins::7::pixel::s1"))
    cache.update(announcement("Wiggins::7::pixel::s2"))  # a new session: same capabilities
    assert len(writes) == 1
    cache.update(announcement("Wiggins::7::pixel::s2", capabilities(ask_timeout_s=30).to_dict()))
    assert len(writes) == 2
    assert CapabilityCache(tmp_path / "c.json").get("7").capabilities.ask_timeout_s == 30


def test_rejection_and_forget_remove_the_saved_phone(tmp_path):
    path = tmp_path / "c.json"
    cache = CapabilityCache(path)
    cache.update(announcement("Wiggins::7::pixel::s"))
    cache.update(announcement("Wiggins::8::tablet::s"))
    cache.update(announcement("Wiggins::7::pixel::s", {**DEFAULT_CAPABILITIES.to_dict(), "version": 2}))
    assert CapabilityCache(path).get("7") is None  # rejections aren't saved
    cache.forget("8")
    assert len(CapabilityCache(path)) == 0


@pytest.mark.parametrize("content", [
    "not json",
    '{"version": 99, "phones": {}}',
    '["a list"]',
])
def test_unreadable_file_starts_empty(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content)
    cache = CapabilityCache(path)
    assert len(cache) == 0
    cache.update(announcement(PEER))  # and is replaced on the next change
    assert CapabilityCache(path).get(PEER) is not None


def test_bad_entries_are_dropped(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 1, "phones": {
        "7": {"peer": PEER, "capabilities": DEFAULT_CAPABILITIES.to_dict(), "updated": 1.0},
        "8": {"peer": "x", "capabilities": {"version": 2}, "updated": 1.0},
        "9": {"capabilities": DEFAULT_CAPABILITIES.to_dict()},
    }}))
    cache = CapabilityCache(path)
    assert [p.client_id for p in cache.phones()] == ["7"]


def test_unwritable_file_keeps_working(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    cache = CapabilityCache(blocker / "c.json")  # its directory can't be made
    assert cache.update(announcement(PEER)) is not None
    assert cache.get(PEER) is not None


def test_no_file_without_a_path(tmp_path):
    cache = CapabilityCache()
    cache.update(announcement(PEER))
    assert cache.path is None


def test_pipeline_remembers_phones_across_a_restart(bus, recorder, make_pipeline, make_phone,
                                                    data_home):
    first = make_pipeline()
    make_phone()
    assert (data_home / "ovos-skill-waggle" / "capabilities.json").exists()
    first.default_shutdown()

    restarted = make_pipeline()
    assert restarted.capabilities.get(PEER) is not None
    # The phone stays connected and doesn't announce again; its requests are still Waggle's.
    utt = utterance("set a timer for 10 minutes")
    assert restarted.match_high(utt.data["utterances"], "en-US", utt) is not None


def test_restart_fetches_apps_on_the_first_utterance(bus, make_pipeline, make_phone):
    first = make_pipeline()
    phone = make_phone(apps=[{"label": "Spotify", "package": "com.spotify.music"}])
    first.default_shutdown()
    restarted = make_pipeline()
    assert restarted.apps.labels("7") is None
    utt = utterance("open spotify")
    assert restarted.match_high(utt.data["utterances"], "en-US", utt) is not None
    assert restarted.apps.labels("7") == ["Spotify"]
    assert [e.request.data["name"] for e in phone.log] == ["apps.list"]
