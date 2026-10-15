# CLAUDE.md

Guidance for Claude Code in ovos-skill-waggle. It's a public repo: keep homelab
hostnames, IPs and private repo names out of code, docs and commit messages. Say
"the hub" for the deployment that installs this package.

## Which docs are authoritative

- **WAGGLE.md** is the wire protocol between the hub and a phone: intents (Android
  activities), queries (reads) and commands (settings the phone changes itself).
  It's **frozen as v1**: adding optional fields, queries, commands, extra types or
  error codes keeps v1.
  Removing anything or changing what it means needs a new version number, and
  the phone client (Wiggins) has to change too. Its "Versioning" section has the
  rules.
- **SPEC.md** is this package's design: requests, the pipeline stage, the
  request/response flow, the persona's tools, hub setup, settings and testing.
  Its "Open questions" section records dated decisions. Update SPEC.md in the
  same commit as any change to behaviour.
- **`waggle/testdata/rule_cases.json`** holds the rule-matcher cases as data.
  Wiggins' Kotlin matcher runs the same file, so any change to it has to pass
  on both sides.
- README.md is the short version: status, install, develop.

## Layout

| Path | What it is |
|---|---|
| `waggle/` | The protocol library: message models and validation (`messages.py`), the rule matcher (`rules.py`), intent, query and command builders, a request/response client, `fake_phone.py` and `fake_hub.py` (the `waggle-fake-hub` CLI for testing phones). It must not import `ovos_skill_waggle`, so it can become its own package later. |
| `ovos_skill_waggle/pipeline.py` | `WagglePipeline`, both an OPM pipeline plugin (`ovos-waggle-pipeline-plugin`, only `-high` matches) and an `OVOSAbstractApplication`. It matches utterances from phones that announced capabilities, asks follow-up questions with `get_response`, and keeps a registry of running plugins (`find_phone`) for the tools, and of loaded toolboxes (`persona_has_tools`), so it can leave notification and location questions to the persona. |
| `ovos_skill_waggle/parsing.py` | All language parsing: domain gates, durations and clock times, names, stopping and dismissing (`_parse_stop`), answers to follow-ups. Only the stage parses language. |
| `ovos_skill_waggle/handlers.py` | `RequestHandlers` run a `waggle:request`: re-check the phone's rules, run lookups, send the intent or command, wait, and report through `waggle:request.response`. They never see an utterance. |
| `ovos_skill_waggle/requests.py`, `actions.py` | Request shapes and param validation, and what each request needs from the phone (`phone_allows`, the pre-check intents). |
| `ovos_skill_waggle/tools.py` | `WaggleToolBox` (`opm.agents.toolbox` entry point `ovos-waggle-tools`): one tool per request for ovos-agentic-loop's `ovos-native-toolcall-loop`. |
| `ovos_skill_waggle/said.py` | Each session's `recognizer_loop:utterance` as the phone sent it, so text messages keep the punctuation ovos-core's normalizer strips. The stage re-parses a `message.compose` match from that wording (`as_said`), and follow-up answers come back through it too. A hub's `raw_utterances` in the message's context (`in_context`) wins over the recording and needs no wait; the recording is the stock-hub fallback. |
| `ovos_skill_waggle/capabilities.py` | The capability cache, keyed by HiveMind client id and kept in a JSON file. `as_heard` limits a phone's rules to one utterance by its `can_ask`, `phone_actions`, `waggle_off` and `ask_methods` context keys; the stage, the tool list and the handlers all judge by that. |
| `ovos_skill_waggle/apps.py`, `agenda.py`, `settings.py` | The app-label catalog used for matching "open X", spoken calendar text, and plugin settings. |
| `ovos_skill_waggle/locale/en-us/` | Dialogs and `.voc` files, English only. They ship through `package-data` in pyproject.toml. |

## Tests

The package targets ovos-core 3.x, and its dependencies are OVOS alphas. The
repo's `.venv` is an old ovos-core 2.x / Python 3.11 environment and won't run
the suite. Make a fresh venv, ideally outside the repo:

```sh
uv venv -p 3.13 /path/to/venv
VIRTUAL_ENV=/path/to/venv uv pip install --prerelease=allow -e '.[test]'
/path/to/venv/bin/python -m pytest -q
```

- Editable installs write `ovos_skill_waggle.egg-info/` into the repo; it's
  gitignored. The entry points (pipeline plugin and toolbox) only exist once
  the package is installed, and `tests/test_end_to_end.py`,
  `tests/test_persona.py` and `tests/test_tools.py` rely on them.
- `tests/conftest.py` points the XDG directories at a temporary directory
  before OVOS is imported, and gives each test its own `XDG_DATA_HOME`, so
  tests never touch the real `~/.config` or share a capability file. Keep new
  tests on the fixtures there: `make_pipeline`, `make_phone` (a `FakePhone` on
  a `FakeBus`), and `recorder`.
- Pass the fake phone `now=NOW` for calendar tests, or it uses the real clock.
  `NOW` is UTC; the stage parses times on the phone's clock, so tests that call
  `parse_utterance` directly convert it to America/Chicago first.
- `make_pipeline` and the end-to-end fixture set `handlers.clock_gap_s` to 0;
  the gap tests in `tests/test_stop.py` set their own.
- End-to-end tests run ovos-core's real `IntentService` and, for the persona,
  the real `PersonaService` and `NativeToolCallEngine` around a scripted model.
  Prefer these over mocks when behaviour depends on ovos-core (converse,
  sessions, end-of-turn markers).
- The minimums in pyproject.toml are the exact alphas the suite was run
  against. After testing against newer alphas, raise them to match.

## Hub-side facts the code depends on

- **Which phone asked a tool.** The loop calls tools with their arguments
  only. ovos-persona runs the loop on its handler's thread, so `tools.py` finds
  the `persona:query` message with `dig_for_message(max_records=64)`. The
  default of 10 frames is too shallow for the real stack. The tool list is
  also computed per call, on the same thread, so each phone is offered only
  what it allows. If a later ovos-persona or loop moves tools to another
  thread, the tools find no phone and none are offered.
- **The toolbox has no bus.** ovos-persona loads toolboxes without one, so the
  tools reach the handlers through the `WagglePipeline` running in the same
  ovos-core process (`find_phone`). A persona served from another process gets
  no phone tools.
- **The loop can't load its brain from config** with ovos-plugin-manager
  2.12.5a1 and ovos-agentic-loop 0.2.3a3. The loop passes `load_chat_plugin` a
  `config` argument that function doesn't take, and ovos-persona never calls
  `set_brain`. The hub works around it with a subclass of the loop in its own
  Claude engine file (in the hub's own deployment), whose `_load_brain` returns the
  engine. Check whether this is still needed when bumping either package.
- **Follow-ups from tools.** ovos-core 3's converse stage only considers the
  session's `converse_handlers`. A stage match adds Waggle there; a persona
  match doesn't. So `_ask` adds the skill to the question's session before
  `get_response`. Note that `Session.activate_skill` only updates
  `active_handlers`, which is a different list.
- **Notification and screen text is untrusted.** It comes from other people and
  apps, and it reaches the persona's model through `get_notifications` and
  `get_phone_screen`. The handler sends it as `data` with a note saying so, and
  replies are always the phone's to confirm (ask card, or its own voice readback).
  Keep both when changing either path. Screen content (text, URL, screenshot) never
  goes in a log line or an error message: `waggle.queries.Screen` keeps it out of
  `repr()`, and its errors name keys and sizes only.
- **What a tool read stays off the bus.** A `waggle:request.response` is a reply to
  the request, so HiveMind sends it down to the phone, and every bus client sees it.
  The tools call `handle(..., emit_data=False)`, so the emitted copy has no `data`;
  the tool gets the full response in-process. A screenshot travels only in
  `RequestResponse.image`, which `to_dict()` leaves out, and reaches the model
  through the tool `Outcome.image` (left out of `str()`), which the hub's own tool
  loop turns into an image block.
- **End of turn.** The utterance handler is registered with
  `handler_info="mycroft.skill.handler"`, so ovos-core emits
  `ovos.utterance.handled`. Plain `waggle:request`s and tool calls never emit
  it, because their caller owns the turn.
- **Capability cache path.** By default
  `$XDG_DATA_HOME/ovos-skill-waggle/capabilities.json`
  (`~/.local/share/...`). The `capabilities_file` setting overrides it, and
  `false` keeps it in memory only. The hub needs a persistent volume there.
  The file is written atomically (temp file and `os.replace`), and only when a
  phone's capabilities change.
- **Raw utterances.** ovos-core 3 listens on `ovos.utterance.handle`, not
  `recognizer_loop:utterance`. The bus client hands Waggle's listener the frame
  as received and ovos-core a forwarded copy, built before either runs, whose
  `data` is a shallow copy: the `utterances` list is shared. The normalizer
  returns a new list rather than editing that one, so the record keeps the
  phone's text. A transformer that edits it in place (OVOS-TRANSFORM-1 allows
  it) races the record: ovos-utterance-corrections-plugin does, but only once it
  has regex or word replacements configured, and the hub configures none. The
  handlers run concurrently, so a text message's match waits up to
  `wait_for_said_s` (0.5 s) for the record. `tests/test_said_real_bus.py` runs
  this on a real `MessageBusClient`. A hub that puts `raw_utterances` in the
  utterance's context makes all of this moot: Waggle reads it from the message
  it's handling, with no record and no wait.
- Settings come from `mycroft.conf` under `intents` → `ovos-waggle-pipeline-plugin`.

## Phone-side facts the code depends on

Checked in AOSP DeskClock's source (as GrapheneOS ships it) and on an Android
16 emulator by the Wiggins side; SPEC.md "Stopping timers and alarms" has the
detail.

- **The Clock drops back-to-back intents.** All its intents go to one
  single-instance activity, which drops one that arrives while a cold start
  is under way, though the phone answers `ok`. The handlers hold any Clock
  intent until `CLOCK_GAP_S` (1 s) after that phone answered its last one.
  Keep new Clock actions in `CLOCK_ACTIONS`.
- **One match only for `DISMISS_ALARM`.** If a search mode other than
  `android.all` matches several alarms, the Clock's picker opens, and it
  crashes on Android 16. So the stage sends `next`, or `time` with a.m./p.m.
  settled; it asks for the half or for a labelled alarm's time rather than
  guess. Labels match case-sensitively and by substring, so spoken labels are
  never sent. `all` is always confirmed (the phone's ask card after a
  read-back, or out loud under a `run` rule). HOUR is 24-hour; never send
  IS_PM.
- **Results are invisible.** The Clock reports matches only to a voice
  interactor, so a phone's `ok` means Android accepted the intent.
  Confirmations are hedged ("I've asked your Clock to …"). Dismissing reaches
  only alarms due within 24 hours, and SNOOZE_DURATION is ignored.
- **No intent cancels a running timer.** `timers.stop` sends `DISMISS_TIMER`
  (ringing timers only, no UI) and then `SHOW_TIMERS`, in that order: the
  Clock covers Wiggins, which then refuses launches.
- **Wiggins launches only while it's on screen** (`launch_failed` otherwise;
  on a locked phone 0.4.0-rc3 asks for an unlock first, and earlier versions
  don't listen at all),
  and needs `<queries>` entries and rules for every action Waggle sends. New
  default rules reach existing installs only through a Wiggins DB migration;
  until then `unmatched: block` makes the stage leave those requests alone.

## Release

The hub installs the wheel `--no-deps`, by URL and sha256. A release went like this (0.4.0, 0.5.0):

1. Run the full suite against the alpha set on the final tree.
2. Bump `version` in pyproject.toml in the release commit. The subject ends
   with `(x.y.z)`, as in `S4: persona tools as an ovos-agentic-loop toolbox (0.5.0)`.
3. Get `main` there with a fast-forward merge (history is linear, no merge
   commits), then push `main`.
4. Create a **lightweight** tag `vX.Y.Z`, as all earlier tags are, and push
   it.
5. Build from a clean copy of the tag, never the working tree: `git archive vX.Y.Z | tar -x -C <empty dir>`,
   then `uv build --wheel` there. Check the wheel's `entry_points.txt` for
   both entry points, and check that the locale files are included.
6. `gh release create vX.Y.Z <wheel> --title "ovos-skill-waggle X.Y.Z" --notes-file <notes>`.
   The asset name is the wheel's own, `ovos_skill_waggle-X.Y.Z-py3-none-any.whl`.
7. Download the asset back from its release URL with `curl -fL`, and hash
   that file. Right after publishing, the URL can return a 404 for a little
   while, and plain `curl -O` saves the "Not Found" body as the wheel, so
   check the size or `python -m zipfile -t` it. The hash should match
   GitHub's asset digest (`gh release view vX.Y.Z --json assets`).
8. Hand the release URL, wheel URL and sha256 to whoever maintains the hub
   pin.

Pushes, tags and releases are outward-facing: ask first. `build/`, `dist/` and
`*.egg-info` are ignored build output.

## Code conventions

- Every module starts with a docstring explaining its job and the reason
  behind it, usually pointing to a SPEC.md section. Comments explain why, not
  what.
- `from __future__ import annotations` and type hints throughout. Frozen
  dataclasses for value types. Validation errors are
  `WaggleError`/`bad_request` with WAGGLE.md error codes.
- Log lines start with `"Waggle: "` and use `ovos_utils.log.LOG`. The
  stage must never raise into the pipeline: parse failures are logged and
  treated as no match.
- Anything spoken is a dialog in `locale/en-us/`, rendered through the
  plugin, never a hard-coded string. Outcomes go through `Outcome` and become
  the response's `summary`.
- Lines are kept to about 100 characters. Names are plain words, and constants
  are upper case at module level.
- Tests use the real OVOS pieces on a `FakeBus` and the fake phone, not mocks
  of the bus.
- Commit messages: an imperative subject, then prose paragraphs on what changed
  and why, and how it was tested, ending with the attribution line the session
  specifies.
