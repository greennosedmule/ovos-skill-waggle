"""A fake Waggle hub, for testing phones such as Wiggins (see WAGGLE.md).

Connected to an OVOS messagebus that hivemind-core is attached to,
:class:`FakeHub` waits for a phone's ``waggle.capabilities``, then sends it
scripted requests as replies to that message (so HiveMind routes them to the
phone) and checks each response against the step's expectation.

A script is a JSON list of steps::

    [{"name": "set alarm",
      "intent": {"action": "android.intent.action.SET_ALARM", "extras": {...}},
      "expect": {"ok": true}},
     {"name": "unknown query", "query": {"name": "sms.read"},
      "expect": {"ok": false, "error": "bad_request"}},
     {"name": "dial", "intent": {...}, "ask": true, "prompt": "Tap Allow or Deny"}]

Each step has exactly one of ``intent`` or ``query``: the request's ``data``,
sent as-is apart from an ``id`` added if missing, so malformed requests can be
scripted. Optional keys: ``expect`` (``ok`` and/or ``error``; without it the
outcome is only reported), ``timeout_s`` (default 5, or the phone's
``ask_timeout_s`` + 5 with ``"ask": true``), ``pause_s`` (wait before the step)
and ``prompt`` (shown before the step, for a human tester). A step with no
response at all fails as ``unreachable``, whatever it expects.

Run ``waggle-fake-hub --help`` (or ``python -m waggle.fake_hub``) for the CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Union

from ovos_bus_client.message import Message
from ovos_utils.log import LOG

from waggle.client import WaggleClient
from waggle.messages import (
    CAPABILITIES, INTENT, QUERY, Capabilities, ErrorCode, Response, WaggleError,
    new_request_id,
)

DEFAULT_TIMEOUT_S = 5.0
ASK_MARGIN_S = 5.0
DEFAULT_ASK_TIMEOUT_S = 15.0  # Wiggins' default, for a phone that doesn't announce one
UNREACHABLE = "unreachable"

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"

_ACTION = "android.intent.action."
_RETURN_TO_PHONE = "If another app opened on the phone, switch back to Wiggins now."

# Covers Wiggins' default rules (WAGGLE.md example plus SHOW_ALARMS and SENDTO sms:)
# and the rejection rules. Steps that bring another app to the front come last,
# with a prompt and pause before the next one, since Android limits launches
# from the background. Outcomes that depend on the user's settings or taps
# have no expectation and are only reported.
DEFAULT_SCRIPT: list[dict] = [
    {"name": "unknown query", "query": {"name": "sms.read", "params": {}},
     "expect": {"ok": False, "error": "bad_request"}},
    {"name": "forbidden content: URI",
     "intent": {"action": _ACTION + "VIEW", "data": "content://contacts/people/1",
                "description": "Waggle test: forbidden URI scheme"},
     "expect": {"ok": False, "error": "bad_request"}},
    {"name": "unknown extra type",
     "intent": {"action": _ACTION + "SET_TIMER", "description": "Waggle test: unknown extra type",
                "extras": {"android.intent.extra.alarm.LENGTH": {"type": "parcelable", "value": 60}}},
     "expect": {"ok": False, "error": "bad_request"}},
    {"name": "action with no rule",
     "intent": {"action": "org.ovos.waggle.test.NO_RULE", "description": "Waggle test: no rule"},
     "expect": {"ok": False, "error": "blocked"}},
    {"name": "apps.list", "query": {"name": "apps.list", "params": {"limit": 5}}},
    {"name": "set alarm",
     "prompt": "Next: a 6:30 AM alarm labelled 'Waggle test' and a 60-second timer; "
               "delete them afterwards.",
     "intent": {"action": _ACTION + "SET_ALARM", "description": "Waggle test: alarm for 6:30 AM",
                "extras": {"android.intent.extra.alarm.HOUR": {"type": "int", "value": 6},
                           "android.intent.extra.alarm.MINUTES": {"type": "int", "value": 30},
                           "android.intent.extra.alarm.MESSAGE": {"type": "string",
                                                                  "value": "Waggle test"},
                           "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": True}}},
     "expect": {"ok": True}},
    {"name": "set timer",
     "intent": {"action": _ACTION + "SET_TIMER", "description": "Waggle test: 60-second timer",
                "extras": {"android.intent.extra.alarm.LENGTH": {"type": "int", "value": 60},
                           "android.intent.extra.alarm.MESSAGE": {"type": "string",
                                                                  "value": "Waggle test"},
                           "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": True}}},
     "expect": {"ok": True}},
    {"name": "dial (ask)", "ask": True,
     "prompt": "Wiggins will ask to dial 555-0100: tap Allow or Deny (or let it time out).",
     "intent": {"action": _ACTION + "DIAL", "data": "tel:+15555550100",
                "description": "Waggle test: dial 555-0100"}},
    {"name": "show alarms", "pause_s": 5, "prompt": _RETURN_TO_PHONE,
     "intent": {"action": _ACTION + "SHOW_ALARMS", "description": "Waggle test: show alarms"},
     "expect": {"ok": True}},
    {"name": "open app", "pause_s": 5, "prompt": _RETURN_TO_PHONE,
     "intent": {"action": _ACTION + "MAIN", "categories": ["android.intent.category.LAUNCHER"],
                "package": "com.android.settings", "description": "Waggle test: open Settings"},
     "expect": {"ok": True}},
]


# --- the phone ---------------------------------------------------------------

@dataclass(frozen=True)
class PhoneInfo:
    """A phone that announced itself; ``capabilities`` is None if they didn't parse."""
    message: Message
    peer: Optional[str]
    capabilities: Optional[Capabilities]
    error: Optional[WaggleError] = None

    @property
    def supported(self) -> bool:
        return self.capabilities is not None

    @property
    def ask_timeout_s(self) -> float:
        caps = self.capabilities
        return float(caps.ask_timeout_s) if caps and caps.ask_timeout_s is not None \
            else DEFAULT_ASK_TIMEOUT_S

    def describe(self) -> str:
        data = self.message.data if isinstance(self.message.data, dict) else {}
        client = data.get("client") if isinstance(data.get("client"), dict) else {}
        name = " ".join(str(client[k]) for k in ("name", "version") if client.get(k))
        status = "ok" if self.supported else f"{self.error.code.value}: {self.error.message}"
        return f"{self.peer or '(no source)'} [{name or 'unknown client'}] " \
               f"version {data.get('version')!r}, {status}"


# --- scripts -----------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    name: str
    kind: str  # INTENT or QUERY
    data: dict
    expect: dict = field(default_factory=dict)
    timeout_s: Optional[float] = None
    ask: bool = False
    pause_s: float = 0.0
    prompt: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Any, index: int = 0) -> "Step":
        """Parse one script step; raises ValueError for a malformed step."""
        where = f"step {index + 1}"
        if not isinstance(d, dict):
            raise ValueError(f"{where} must be an object")
        name = d.get("name", where)
        kinds = [k for k in ("intent", "query") if k in d]
        if len(kinds) != 1:
            raise ValueError(f"{where} ({name}) needs exactly one of 'intent' or 'query'")
        data = d[kinds[0]]
        if not isinstance(data, dict):
            raise ValueError(f"{where} ({name}): '{kinds[0]}' must be an object")
        expect = d.get("expect") or {}
        if not isinstance(expect, dict) or set(expect) - {"ok", "error"}:
            raise ValueError(f"{where} ({name}): 'expect' may only have 'ok' and 'error'")
        if "ok" in expect and not isinstance(expect["ok"], bool):
            raise ValueError(f"{where} ({name}): expect 'ok' must be true or false")
        if expect.get("error") is not None and not isinstance(expect["error"], str):
            raise ValueError(f"{where} ({name}): expect 'error' must be a string")
        for key in ("timeout_s", "pause_s"):
            value = d.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or value < 0):
                raise ValueError(f"{where} ({name}): {key!r} must be a number >= 0")
        prompt = d.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError(f"{where} ({name}): 'prompt' must be a string")
        return cls(name=str(name), kind=INTENT if kinds[0] == "intent" else QUERY,
                   data=dict(data), expect=dict(expect), timeout_s=d.get("timeout_s"),
                   ask=bool(d.get("ask", False)), pause_s=d.get("pause_s") or 0.0, prompt=prompt)


def parse_script(script: Any) -> list[Step]:
    """Parse a script (a list of step dicts, or Steps); raises ValueError."""
    if not isinstance(script, list):
        raise ValueError("a script must be a list of steps")
    return [s if isinstance(s, Step) else Step.from_dict(s, i) for i, s in enumerate(script)]


def load_script(path: str) -> list[Step]:
    with open(path, encoding="utf-8") as f:
        return parse_script(json.load(f))


# --- results -----------------------------------------------------------------

@dataclass(frozen=True)
class StepResult:
    step: Step
    request: dict  # the data actually sent, with its id
    response: Optional[Response]
    elapsed_s: float
    passed: Optional[bool]  # None: nothing expected, only reported
    reason: Optional[str] = None  # why it failed

    @property
    def status(self) -> str:
        return INFO if self.passed is None else PASS if self.passed else FAIL

    @property
    def outcome(self) -> str:
        """``ok``, the response's error code, or ``unreachable``."""
        if self.response is None:
            return UNREACHABLE
        return "ok" if self.response.ok else _code(self.response.error)

    def to_dict(self) -> dict:
        return {"name": self.step.name, "type": self.step.kind, "request": self.request,
                "expect": self.step.expect, "status": self.status, "outcome": self.outcome,
                "response": self.response.to_dict() if self.response else None,
                "reason": self.reason, "elapsed_s": round(self.elapsed_s, 3)}


def _code(error: Union[ErrorCode, str, None]) -> Optional[str]:
    return error.value if isinstance(error, ErrorCode) else error


def check(step: Step, response: Optional[Response]) -> tuple[Optional[bool], Optional[str]]:
    """(passed, reason) for a step's response; passed is None if nothing was expected."""
    if response is None:
        return False, f"no response ({UNREACHABLE})"
    if not step.expect:
        return None, None
    problems = []
    if "ok" in step.expect and response.ok != step.expect["ok"]:
        problems.append(f"expected ok={step.expect['ok']}, got ok={response.ok}")
    if "error" in step.expect and _code(response.error) != step.expect["error"]:
        problems.append(f"expected error={step.expect['error']}, got error={_code(response.error)}")
    return (False, "; ".join(problems)) if problems else (True, None)


# --- the hub -----------------------------------------------------------------

class FakeHub:
    """Records phones' capabilities as they arrive and runs scripts against them.

    It starts listening for ``waggle.capabilities`` when created, so a phone
    that announced before :meth:`wait_for_phone` is still found.
    """

    def __init__(self, bus, sleep: Optional[Callable[[float], None]] = None):
        self.bus = bus
        self.client = WaggleClient(bus)
        self.sleep = sleep or (lambda s: time.sleep(s))  # looked up late, so tests can patch it
        self.phones: list[PhoneInfo] = []
        self._changed = threading.Condition()
        bus.on(CAPABILITIES, self._on_capabilities)

    def close(self) -> None:
        self.bus.remove(CAPABILITIES, self._on_capabilities)

    def _on_capabilities(self, message: Message) -> None:
        try:
            capabilities, error = Capabilities.from_dict(message.data), None
        except WaggleError as e:
            capabilities, error = None, e
            LOG.warning(f"Phone {message.context.get('source')!r} announced capabilities "
                        f"we can't use: {e.code.value}: {e.message}")
        phone = PhoneInfo(message, message.context.get("source"), capabilities, error)
        with self._changed:
            self.phones.append(phone)
            self._changed.notify_all()

    def wait_for_phone(self, timeout_s: float, peer: Optional[str] = None) -> PhoneInfo:
        """The latest phone whose peer contains ``peer``, waiting for one if needed.

        A phone with an unsupported version is returned too (``supported`` is
        False). Raises TimeoutError if no phone announces in time.
        """
        deadline = time.monotonic() + timeout_s

        def latest() -> Optional[PhoneInfo]:
            for phone in reversed(self.phones):
                if peer is None or (phone.peer is not None and peer in str(phone.peer)):
                    return phone
            return None

        with self._changed:
            while (found := latest()) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no waggle.capabilities within {timeout_s}s"
                                       + (f" from a peer matching {peer!r}" if peer else ""))
                self._changed.wait(remaining)
            return found

    def run(self, script: Iterable[Union[dict, Step]], phone: PhoneInfo,
            on_prompt: Optional[Callable[[str], None]] = None,
            on_result: Optional[Callable[[StepResult], None]] = None) -> list[StepResult]:
        """Run each step against ``phone`` in turn; raises ValueError for a bad script
        or a phone whose capabilities we can't use (WAGGLE.md: never send to it)."""
        steps = parse_script(list(script))
        if not phone.supported:
            raise ValueError(f"won't send to phone {phone.peer!r}: {phone.error.code.value}")
        results = []
        for step in steps:
            if step.pause_s:
                self.sleep(step.pause_s)
            if step.prompt:
                (on_prompt or LOG.info)(step.prompt)
            result = self.run_step(step, phone)
            results.append(result)
            if on_result:
                on_result(result)
        return results

    def run_step(self, step: Step, phone: PhoneInfo) -> StepResult:
        timeout_s = step.timeout_s
        if timeout_s is None:
            timeout_s = phone.ask_timeout_s + ASK_MARGIN_S if step.ask else DEFAULT_TIMEOUT_S
        request = dict(step.data)
        if "id" not in request:
            request["id"] = new_request_id()
        started = time.monotonic()
        response = self.client.send_raw(phone.message, step.kind, request, timeout_s)
        passed, reason = check(step, response)
        return StepResult(step, request, response, time.monotonic() - started, passed, reason)


# --- reporting -----------------------------------------------------------------

def summarize_request(kind: str, data: dict) -> str:
    """A one-line summary of a request's data, e.g. ``intent SET_ALARM extras[HOUR,MINUTES]``."""
    if kind == QUERY:
        name = data.get("name")
        params = data.get("params")
        return f"query {name}" + (f" {json.dumps(params, sort_keys=True)}" if params else "")
    action = data.get("action")
    parts = ["intent", action.rsplit(".", 1)[-1] if isinstance(action, str) else repr(action)]
    if data.get("data") is not None:
        parts.append(str(data["data"]))
    categories = data.get("categories")
    if isinstance(categories, list) and categories:
        parts.append("+" + ",".join(str(c).rsplit(".", 1)[-1] for c in categories))
    if data.get("package"):
        parts.append(f"pkg={data['package']}")
    extras = data.get("extras")
    if isinstance(extras, dict) and extras:
        parts.append("extras[" + ",".join(str(k).rsplit(".", 1)[-1] for k in extras) + "]")
    return " ".join(parts)


def summarize_response(response: Optional[Response], limit: int = 80) -> str:
    if response is None:
        return UNREACHABLE
    if not response.ok:
        text = _code(response.error)
        return text + (f" ({response.message})" if response.message else "")
    if not response.data:
        return "ok"
    data = json.dumps(response.data, sort_keys=True)
    return "ok " + (data if len(data) <= limit else data[:limit - 3] + "...")


def format_result(result: StepResult) -> str:
    line = f"{result.status:4}  {result.step.name}: {summarize_request(result.step.kind, result.request)}" \
           f" -> {summarize_response(result.response)} ({result.elapsed_s:.2f}s)"
    return line + (f"  [{result.reason}]" if result.reason else "")


def failed(results: Iterable[StepResult]) -> list[StepResult]:
    return [r for r in results if r.passed is False]


def summary_line(results: list[StepResult]) -> str:
    counts = {s: sum(r.status == s for r in results) for s in (PASS, FAIL, INFO)}
    return f"{len(results)} steps: {counts[PASS]} passed, {counts[FAIL]} failed, " \
           f"{counts[INFO]} reported"


def json_report(phone: Optional[PhoneInfo], results: list[StepResult],
                error: Optional[str] = None) -> dict:
    return {
        "ok": error is None and not failed(results),
        "error": error,
        "phone": None if phone is None else {
            "peer": phone.peer, "supported": phone.supported,
            "error": _code(phone.error.code) if phone.error else None,
            "capabilities": phone.message.data,
        },
        "steps": [r.to_dict() for r in results],
    }


# --- CLI -----------------------------------------------------------------------

EXIT_OK, EXIT_FAILED, EXIT_SETUP = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waggle-fake-hub",
        description="Send scripted Waggle requests to a phone through a local OVOS messagebus "
                    "with hivemind-core attached, and check its responses.")
    parser.add_argument("--host", default="127.0.0.1", help="messagebus host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8181, help="messagebus port (default: %(default)s)")
    parser.add_argument("--route", default="/core", help="messagebus route (default: %(default)s)")
    parser.add_argument("--script", metavar="PATH",
                        help="JSON script of steps (default: the built-in script)")
    parser.add_argument("--peer", metavar="SUBSTRING",
                        help="use the phone whose HiveMind peer contains this")
    parser.add_argument("--wait-s", type=float, default=120.0,
                        help="how long to wait for waggle.capabilities (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    return parser


def _connect(args: argparse.Namespace):
    from ovos_bus_client import MessageBusClient
    bus = MessageBusClient(host=args.host, port=args.port, route=args.route)
    bus.run_in_thread()
    if not bus.connected_event.wait(10):
        bus.close()
        raise ConnectionError(f"can't connect to ws://{args.host}:{args.port}{args.route}")
    return bus


def main(argv: Optional[list[str]] = None, bus=None, out=None, err=None) -> int:
    """The ``waggle-fake-hub`` CLI; ``bus`` (and the streams) can be injected for tests."""
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    err = err or sys.stderr
    # Prompts and progress go to stderr with --json, so stdout stays parseable.
    human = err if args.json else out

    def say(text: str, stream=None) -> None:
        print(text, file=stream or human, flush=True)

    def finish(phone: Optional[PhoneInfo], results: list[StepResult],
               error: Optional[str] = None) -> int:
        if args.json:
            print(json.dumps(json_report(phone, results, error), indent=2), file=out)
        elif error:
            say(f"ERROR: {error}", err)
        else:
            say(summary_line(results))
        if error:
            return EXIT_SETUP
        return EXIT_FAILED if failed(results) else EXIT_OK

    try:
        steps = load_script(args.script) if args.script else parse_script(DEFAULT_SCRIPT)
    except (OSError, ValueError) as e:
        return finish(None, [], f"bad script: {e}")

    owns_bus = bus is None
    if owns_bus:
        try:
            bus = _connect(args)
        except Exception as e:
            return finish(None, [], str(e))
    hub = FakeHub(bus)
    try:
        say(f"Waiting up to {args.wait_s:g}s for waggle.capabilities"
            + (f" from a peer matching {args.peer!r}" if args.peer else "")
            + " (connect or reconnect the phone)...", err)
        try:
            phone = hub.wait_for_phone(args.wait_s, args.peer)
        except TimeoutError as e:
            return finish(None, [], str(e))
        say(f"Phone: {phone.describe()}")
        if not phone.supported:
            return finish(phone, [], f"phone announced capabilities we can't use: "
                                     f"{phone.error.code.value}: {phone.error.message}")
        results = hub.run(steps, phone, on_prompt=lambda text: say(f">>> {text}"),
                          on_result=None if args.json else lambda r: say(format_result(r)))
        return finish(phone, results)
    finally:
        hub.close()
        if owns_bus:
            bus.close()


if __name__ == "__main__":
    sys.exit(main())
