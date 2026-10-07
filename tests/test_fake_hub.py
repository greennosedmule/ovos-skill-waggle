"""FakeHub and its CLI, run against a FakePhone on a FakeBus."""
from __future__ import annotations

import io
import json
import threading

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

from waggle.fake_hub import (
    DEFAULT_SCRIPT, EXIT_FAILED, EXIT_OK, EXIT_SETUP, FAIL, INFO, PASS, FakeHub, Step,
    build_parser, format_result, main, parse_script, summarize_request,
)
from waggle.fake_phone import AskAnswer, FakePhone
from waggle.messages import CAPABILITIES, INTENT, QUERY, ErrorCode

APPS = [{"label": "Settings", "package": "com.android.settings"}]


@pytest.fixture
def bus():
    return FakeBus()


@pytest.fixture
def hub(bus):
    hub = FakeHub(bus, sleep=lambda s: None)
    yield hub
    hub.close()


def test_wait_for_phone_records_capabilities(bus, hub):
    phone = FakePhone(bus, peer="wiggins::abc::Pixel::s1")
    phone.announce()
    info = hub.wait_for_phone(0.5)
    assert info.supported and info.capabilities == phone.capabilities
    assert info.peer == "wiggins::abc::Pixel::s1"
    assert info.message.msg_type == CAPABILITIES
    assert info.ask_timeout_s == 15


def test_wait_for_phone_blocks_until_announce(bus, hub):
    phone = FakePhone(bus)
    timer = threading.Timer(0.05, phone.announce)
    timer.start()
    assert hub.wait_for_phone(1).peer == "fake-phone"


def test_wait_for_phone_peer_filter_and_timeout(bus, hub):
    FakePhone(bus, peer="wiggins::alice").announce()
    FakePhone(bus, peer="wiggins::bob").announce()
    assert hub.wait_for_phone(0.1, peer="alice").peer == "wiggins::alice"
    assert hub.wait_for_phone(0.1).peer == "wiggins::bob"  # the latest
    with pytest.raises(TimeoutError):
        hub.wait_for_phone(0.05, peer="carol")


def test_unsupported_version_reported(bus, hub):
    bus.emit(Message(CAPABILITIES, {"version": 99}, {"source": "future-phone"}))
    info = hub.wait_for_phone(0.1)
    assert not info.supported and info.error.code is ErrorCode.UNSUPPORTED_VERSION
    assert "unsupported_version" in info.describe()
    with pytest.raises(ValueError):
        hub.run(DEFAULT_SCRIPT, info)


def test_default_script_passes_against_fake_phone(bus, hub):
    phone = FakePhone(bus, apps=APPS)
    phone.announce()
    prompts, streamed = [], []
    results = hub.run(DEFAULT_SCRIPT, hub.wait_for_phone(0.5), on_prompt=prompts.append,
                      on_result=streamed.append)
    assert [r.status for r in results if r.step.expect] == [PASS] * sum(
        bool(s.get("expect")) for s in DEFAULT_SCRIPT)
    assert all(r.status == INFO for r in results if not r.step.expect)
    assert all(r.response is not None for r in results)
    assert streamed == results
    assert len(prompts) == sum("prompt" in s for s in DEFAULT_SCRIPT)
    assert len(phone.log) == len(DEFAULT_SCRIPT)
    # The ask step was really an ask, and requests went to the phone that announced.
    dial = next(e for e in phone.log if e.request.data.get("action", "").endswith("DIAL"))
    assert dial.decision.mode.value == "ask"
    assert all(e.request.context["destination"] == "fake-phone" for e in phone.log)


def test_default_script_has_the_promised_steps():
    names = {s["name"] for s in DEFAULT_SCRIPT}
    assert {"set alarm", "set timer", "show alarms", "open app", "dial (ask)",
            "forbidden content: URI", "unknown extra type", "unknown query",
            "action with no rule", "apps.list"} <= names
    assert len(parse_script(DEFAULT_SCRIPT)) == len(DEFAULT_SCRIPT)


def test_failing_expectation_reported(bus, hub):
    FakePhone(bus, launch_by_action={"android.intent.action.SET_ALARM": ErrorCode.NO_HANDLER}).announce()
    script = [
        {"name": "alarm", "intent": {"action": "android.intent.action.SET_ALARM"},
         "expect": {"ok": True}},
        {"name": "wrong code", "intent": {"action": "x.NO_RULE"},
         "expect": {"error": "declined"}},
        {"name": "right code", "intent": {"action": "x.NO_RULE"},
         "expect": {"ok": False, "error": "blocked"}},
    ]
    results = hub.run(script, hub.wait_for_phone(0.5))
    assert [r.status for r in results] == [FAIL, FAIL, PASS]
    assert results[0].outcome == "no_handler"
    assert "expected ok=True, got ok=False" in results[0].reason
    assert "expected error=declined, got error=blocked" in results[1].reason
    assert format_result(results[0]).startswith("FAIL  alarm: intent SET_ALARM -> no_handler")


def test_no_response_is_unreachable_failure(bus, hub):
    phone = FakePhone(bus)
    phone.announce()
    info = hub.wait_for_phone(0.5)
    phone.silent = True
    results = hub.run([{"name": "s", "query": {"name": "apps.list"}, "timeout_s": 0.05}], info)
    assert results[0].status == FAIL and results[0].outcome == "unreachable"


def test_ask_step_waits_for_ask_timeout(bus, hub):
    FakePhone(bus, ask_answer=AskAnswer.NONE, timeout_delay_s=0.1).announce()
    info = hub.wait_for_phone(0.5)
    step = {"name": "dial", "intent": {"action": "android.intent.action.DIAL", "data": "tel:1"},
            "expect": {"error": "timeout"}}
    assert hub.run([{**step, "timeout_s": 0.02}], info)[0].outcome == "unreachable"
    assert hub.run([{**step, "ask": True}], info)[0].status == PASS  # waits 15 + 5 s at most


def test_ids_added_only_when_missing(bus, hub):
    phone = FakePhone(bus)
    phone.announce()
    info = hub.wait_for_phone(0.5)
    results = hub.run([
        {"name": "auto", "query": {"name": "apps.list"}},
        {"name": "given", "query": {"id": "mine", "name": "apps.list"}},
        {"name": "bad id", "query": {"id": 42, "name": "apps.list"},
         "expect": {"error": "bad_request"}},
    ], info)
    assert results[0].request["id"] and results[1].response.id == "mine"
    assert results[2].status == PASS and phone.log[2].request.data["id"] == 42


def test_pause_uses_injected_sleep(bus):
    slept = []
    hub = FakeHub(bus, sleep=slept.append)
    FakePhone(bus).announce()
    hub.run([{"name": "p", "pause_s": 3, "query": {"name": "apps.list"}}], hub.wait_for_phone(0.5))
    assert slept == [3]


@pytest.mark.parametrize("script", [
    {"name": "not a list"},
    [{"name": "neither"}],
    [{"name": "both", "intent": {}, "query": {}}],
    [{"name": "not object", "intent": "SET_ALARM"}],
    [{"name": "bad expect", "query": {}, "expect": {"ok": "yes"}}],
    [{"name": "extra expect", "query": {}, "expect": {"data": {}}}],
    [{"name": "bad timeout", "query": {}, "timeout_s": -1}],
])
def test_bad_scripts(script):
    with pytest.raises(ValueError):
        parse_script(script)


def test_step_defaults():
    step = Step.from_dict({"query": {"name": "apps.list"}}, 2)
    assert (step.name, step.kind, step.expect, step.ask, step.pause_s) == \
        ("step 3", QUERY, {}, False, 0.0)


def test_summarize_request():
    assert summarize_request(INTENT, DEFAULT_SCRIPT[-1]["intent"]) == \
        "intent MAIN +LAUNCHER pkg=com.android.settings"
    assert summarize_request(INTENT, {"action": "android.intent.action.SET_ALARM", "extras": {
        "android.intent.extra.alarm.HOUR": {}, "android.intent.extra.alarm.MINUTES": {}}}) == \
        "intent SET_ALARM extras[HOUR,MINUTES]"
    assert summarize_request(INTENT, {"action": "x.DIAL", "data": "tel:1"}) == "intent DIAL tel:1"
    assert summarize_request(QUERY, {"name": "apps.list", "params": {"limit": 5}}) == \
        'query apps.list {"limit": 5}'
    assert summarize_request(INTENT, {"action": 7}) == "intent 7"


# --- CLI ---------------------------------------------------------------------

def test_parser_defaults_and_options():
    args = build_parser().parse_args([])
    assert (args.host, args.port, args.route, args.script, args.peer, args.wait_s, args.json) == \
        ("127.0.0.1", 8181, "/core", None, None, 120.0, False)
    args = build_parser().parse_args(["--host", "hub", "--port", "9000", "--route", "/x",
                                      "--script", "s.json", "--peer", "wig", "--wait-s", "3",
                                      "--json"])
    assert (args.host, args.port, args.route, args.script, args.peer, args.wait_s, args.json) == \
        ("hub", 9000, "/x", "s.json", "wig", 3.0, True)


def run_cli(bus, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), bus=bus, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def announce_soon(phone):
    threading.Timer(0.02, phone.announce).start()


def test_cli_default_script_text_report(bus, monkeypatch):
    monkeypatch.setattr("waggle.fake_hub.time.sleep", lambda s: None)
    announce_soon(FakePhone(bus, apps=APPS))
    code, out, err = run_cli(bus, "--wait-s", "1")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0].startswith("Phone: fake-phone [waggle-fake-phone 1] version 1, ok")
    step_lines = [l for l in lines if l[:4] in (PASS, FAIL, INFO)]
    assert len(step_lines) == len(DEFAULT_SCRIPT)
    assert any(l.startswith("PASS  set alarm: intent SET_ALARM extras[") and l.endswith("s)")
               for l in step_lines)
    assert any(l.startswith(">>> ") for l in lines)
    assert lines[-1] == f"{len(DEFAULT_SCRIPT)} steps: 8 passed, 0 failed, 2 reported"
    assert "Waiting up to 1s" in err


def test_cli_json_report_and_failure_exit(bus, tmp_path):
    script = tmp_path / "s.json"
    script.write_text(json.dumps([
        {"name": "ok", "intent": {"action": "android.intent.action.SET_TIMER"}, "expect": {"ok": True}},
        {"name": "bad", "intent": {"action": "x.NO_RULE"}, "expect": {"ok": True}},
    ]))
    announce_soon(FakePhone(bus, peer="wiggins::bob"))
    code, out, err = run_cli(bus, "--json", "--script", str(script), "--peer", "bob",
                             "--wait-s", "1")
    assert code == EXIT_FAILED
    report = json.loads(out)
    assert report["ok"] is False and report["phone"]["peer"] == "wiggins::bob"
    assert [s["status"] for s in report["steps"]] == [PASS, FAIL]
    assert report["steps"][1]["outcome"] == "blocked"
    assert report["steps"][1]["response"]["error"] == "blocked"
    assert "Phone: wiggins::bob" in err


def test_cli_no_phone(bus):
    code, out, err = run_cli(bus, "--wait-s", "0.05")
    assert code == EXIT_SETUP and "ERROR: no waggle.capabilities" in err


def test_cli_unsupported_phone_json(bus):
    threading.Timer(0.02, bus.emit, [Message(CAPABILITIES, {"version": 2}, {"source": "p"})]).start()
    code, out, _ = run_cli(bus, "--json", "--wait-s", "1")
    report = json.loads(out)
    assert code == EXIT_SETUP and report["phone"]["error"] == "unsupported_version"
    assert report["steps"] == []


def test_cli_bad_script(bus, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    code, _, err = run_cli(bus, "--script", str(path))
    assert code == EXIT_SETUP and "bad script" in err
    code, _, err = run_cli(bus, "--script", str(tmp_path / "missing.json"))
    assert code == EXIT_SETUP
