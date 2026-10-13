# ovos-skill-waggle — Spec

Oct 6, 2026 · @greennosedmule

## Purpose and scope

An OVOS pipeline plugin and skill that turn requests like "set an alarm for 6:30" into Waggle messages for Wiggins, the HiveMind Android client: generic Android intents for actions, named queries for reads. It waits for the phone's answer and speaks the result. Parsing and intent-building happen on the hub; the phone only executes.

The message format is the Waggle protocol, defined in [WAGGLE.md](WAGGLE.md); any HiveMind client that speaks it can use this package. Out of scope: requests for the phone from anywhere but the phone itself, such as another satellite or a home automation (decided 2026-10-12), and anything the hub can do itself without the phone, such as controlling Home Assistant.

**Who it answers.** A request spoken to a phone is meant for that phone: "set a timer" said to Wiggins should ring on the phone, not on the hub. So for a phone that speaks Waggle, Waggle takes its requests ahead of every other skill, and for every other satellite it does nothing, leaving the stock skills (the alerts skill's hub timers) as they are.

## Repository layout

The repository holds three things that may later split apart:

| Path | What it is |
| --- | --- |
| `WAGGLE.md` | The protocol spec (canonical) |
| `waggle/` | Python library: message models and validation, the rule matcher, Android intent builders, a request/response client, and a fake phone and fake hub for tests |
| `ovos_skill_waggle/` | The pipeline stage (matching), the request handlers, the capability cache, the persona's toolbox, dialogs and locale files, settings |

The library has no dependency on the rest, so that it can move to its own package once a second skill or client uses it. Until then one repository and one pip package keep things simple. The fake phone tests the handlers; the fake hub tests Wiggins by sending scripted Waggle requests and checking the responses.

## Design: matching is separate from handling

Two layers, so that an utterance and an LLM can ask for the same thing:

1. **Requests** (the handlers) take a structured request, such as `timer.set {seconds: 600}`, for a phone peer, and do everything else: check the phone's rules, look things up on the phone, send the Waggle messages, wait, and report the outcome. They never see the utterance.
2. **The pipeline stage** turns an utterance from a Waggle phone into a request. It is the only part that parses language.

Anything else can make requests too: the persona's tool calls (S4), or another skill.

### Requests

A request is the bus message `waggle:request`:

```json
{"type": "waggle:request", "data": {
  "request": "timer.set",
  "params": {"seconds": 600},
  "speak": true
}, "context": {"…": "the original utterance's context, so replies reach the same peer"}}
```

| Request | Params | Waggle messages sent | User hears |
| --- | --- | --- | --- |
| `alarm.set` | `hour`, `minute` (phone's wall clock), optional `label` | intent `SET_ALARM`, extras HOUR, MINUTES, SKIP\_UI | "Alarm set for 6:30 AM." |
| `timer.set` | `seconds`, optional `label` | intent `SET_TIMER`, extras LENGTH, SKIP\_UI | "10-minute timer started." |
| `alarms.show` | — | intent `SHOW_ALARMS` | "Here are your alarms." |
| `calendar.next` | optional `count` (1–10, default 1) or `day` (`YYYY-MM-DD` on the phone's calendar) | query `calendar.next` | "Next on your calendar: Dentist, tomorrow at 2 PM." |
| `app.open` | `name` | query `apps.list` {name}, then intent `MAIN` + `LAUNCHER` with the matched package | "Opening Camera." |
| `contact.call` | `name`, optional `type` (`cell`, `home`, `work`, `other`) | query `contacts.lookup`, then intent `DIAL` with a `tel:` URI | "Dialing Mom. Tap call to connect." |
| `message.compose` | `name`, `body` | query `contacts.lookup`, then intent `SENDTO` with an `smsto:` URI and `sms_body` | "Message ready. Tap send on your phone." |
| `timers.stop` | — | intent `DISMISS_TIMER`, then intent `SHOW_TIMERS`, each if the phone allows it | "I've stopped any ringing timer. Here are your timers." |
| `alarm.dismiss` | `mode` (`next`, the default; `time` with `hour` and `minute`; `label` with `label`; `all`) | intent `DISMISS_ALARM`, extras SEARCH\_MODE (`android.next`, `android.time` with HOUR and MINUTES, `android.label` with MESSAGE, `android.all`) | "I've asked your Clock to dismiss the 6:30 AM alarm." |
| `alarm.snooze` | optional `minutes` (1–60) | intent `SNOOZE_ALARM`, extra SNOOZE\_DURATION when given | "I've asked your Clock to snooze the alarm." |

A `name` that is a phone number said aloud ("call 555 1234") is dialed or texted without a lookup. `timers.stop` is allowed when either of its intents is; it skips the one the phone blocks, still shows the timers when the Clock has no `DISMISS_TIMER` (`no_handler`), and stops at a declined or unanswered ask card. A `day` question fetches up to ten events and keeps those on that day. Each handler builds its Android intent with a `waggle` builder, so the handlers are mostly a library of intent builders. When it finishes, the handler emits `waggle:request.response` with `{"ok": ..., "error"?: <WAGGLE.md code or "unreachable">, "summary": <what happened, in words>}`, as a reply to the request. With `speak: true` (the pipeline's requests) it also speaks the outcome; with `speak: false` (the persona's tool calls) the caller says it in its own words. Either way, an `ask` rule's "Confirm on your phone." is spoken when the intent is sent, since the user has to act before there is an outcome to report.

### The pipeline stage

The stage is an ovos-core 3.x pipeline plugin, `ovos-waggle-pipeline-plugin`. It goes near the front of the hub's pipeline, after the stop and converse stages (so a skill's follow-up question, Waggle's own included, still gets its answer) and before every stage that could take a phone request, such as Adapt, Padatious and the alerts skill:

```json
"pipeline": ["ovos-stop-pipeline-plugin-high", "ovos-converse-pipeline-plugin",
             "ovos-waggle-pipeline-plugin-high", "ovos-ocp-pipeline-plugin-high",
             "ovos-padatious-pipeline-plugin-high", "ovos-adapt-pipeline-plugin-high", "…"]
```

Only the `-high` tier matches: the stage has one place in the pipeline, and its matches don't come in degrees (`-medium` and `-low` never match).

It matches only when all of these hold, and otherwise returns no match, so the rest of the pipeline runs as if Waggle weren't installed:

1. **The speaker is a Waggle phone:** the utterance came from a peer that has announced `waggle.capabilities` in a `version` this package supports. Satellites that don't speak Waggle never announce, so they're never matched, and no list of client IDs is kept on the hub.
2. **The utterance is a request it understands.** Matching is deliberately generous for phones in Waggle's domains (times, alarms, timers): a phrasing the stage misses would go on to the alerts skill and set a timer on the hub, which is the wrong place. A false match costs little, since the phone's rules and ask card still decide.
3. **The phone would allow it,** by the WAGGLE.md rule matcher run on the cached capabilities. An intent the phone blocks (by a rule, or `unmatched: block` with no rule) isn't Waggle's to take: the user chose not to allow it on the phone, so the request goes on to the stock skills and is handled on the hub. A query the phone hasn't enabled is treated the same way. An `ask` rule matches; the phone asks.

A match produces a `waggle:request` with `speak: true`. ovos-core emits a match as a reply to the utterance, so it arrives as the match type `waggle:utterance`, whose data is a `waggle:request`'s (`request`, `params`, `speak`) plus the utterance fields. The handler runs it exactly like a `waggle:request` and is registered with `is_intent=True` and `handler_info="mycroft.skill.handler"`, so `ovos.utterance.handled` follows once the outcome has been spoken; it goes to the phone, ending its turn. (ovos-workshop reports `mycroft.skill.handler.complete`, or `.error`, and ovos-core's dispatcher emits the end marker.) Plain `waggle:request`s and the persona's tool calls never emit it, since whoever made the request (the persona, another skill) owns the utterance.

Matching is keywords and patterns, not an intent engine: a gate per domain ("timer", "alarm", "wake me", "my alarms", with set verbs and without cancel, snooze and the like, which have their own gates below), then durations through ovos-date-parser's `extract_duration` after rewriting the phrasings it misses ("an hour and a half", "a minute", "10 mins"), and clock times through patterns for "6:30 a.m.", "18:30", "half past six", "quarter to seven", "seven fifteen pm", "noon" and so on, with ovos-date-parser's `extract_datetime` as a last resort. When a.m. or p.m. isn't said, "wake me", "morning" and "tomorrow" mean a.m.; otherwise it's the next time the phone's clock shows that time. `SET_ALARM` has no date, so an alarm for another day ("Friday at 7", "every weekday") isn't taken. "Set a timer" or "set an alarm" without a length or time is taken and asks for it (see below).

The other gates take calendar questions (a calendar word with "my", "any", "next" or "upcoming" and a question word, or "what do I have" and "am I free" with a day; adding, moving and cancelling events aren't taken), calls ("call", "phone", "dial", "give … a call", with "on her cell", "at work" or "dad's work number" choosing the number), texts ("text Mom saying …", "send a message to Dad: …", "message Jenny that …", and "text Mom I'm late", where the first word is taken as the name) and apps ("open", "launch", "start" …). A text keeps the case and punctuation it was said with. ovos-core's utterance normalizer sends its rewrite of each transcription first ("I am late , sorry") and the original after it, but strips leading and trailing punctuation from both ("I'm late, sorry"). So the plugin also listens to `recognizer_loop:utterance`, which the bus hands each listener as its own copy before any transformer runs, and keeps each session's last few utterances as the phone sent them for two minutes. A message body is taken from the one the matched utterance reads as (nearly the same letters and digits), both inline and in an answer to "What should the message say?". The bus may run that listener alongside ovos-core's, so a text's match waits up to half a second for it. Without one (an utterance that never came as `recognizer_loop:utterance`), the body comes from the normalizer's original, without its final punctuation.

**Stopping timers and alarms.** What Android's Clock offers (`AlarmClock`; checked in the source of AOSP's DeskClock as GrapheneOS ships it) decides what's taken:

- `DISMISS_TIMER` (API 28) silences every ringing timer and leaves running ones alone, and no intent cancels a running timer. So "stop the timer", "cancel my timer", "cancel the pasta timer", "turn the timer off" and the like are all `timers.stop`: silence whatever rings, then show the timers (`SHOW_TIMERS`, API 26) so the user taps to cancel a running one. A timer can't be picked by its label.
- `DISMISS_ALARM` (API 23) opens the Clock on its alarms and, for the alarms its search mode picks, stops one that's ringing or snoozed and skips the next ring of one due within 24 hours; it never deletes an alarm, and can't touch one further away. Several matches (except with `all`) bring up the Clock's own picker. "Cancel my alarm", "turn off the alarm" and "stop the alarm" are `next` (the Clock picks a ringing alarm first); "cancel my 6:30 alarm" and "the alarm at 7" are `time`, read like an alarm being set (the next time the clock shows it unless a.m., p.m., "morning" or "tomorrow" says otherwise); "my work alarm" and "the alarm called gym" are `label` (the Clock matches part of the label, case-sensitively); "all my alarms" or a plural is `all`. Another day ("my Monday alarm") isn't taken, nor are smoke, car and other alarms that aren't the Clock's.
- `SNOOZE_ALARM` (API 23) snoozes every ringing alarm: "snooze", "snooze the alarm", "snooze for 10 minutes". The length goes in SNOOZE\_DURATION, which AOSP's Clock ignores in favour of its own setting.

The Clock reports whether anything matched only to a voice interactor, which Waggle isn't, so a phone's `ok` means only that Android accepted the intent, and the confirmations are hedged ("I've asked your Clock to …"); the Clock opening on screen shows the rest. A bare "stop" or "cancel" isn't taken: OVOS's stop stage runs first and matches exactly those words, and passes "stop the timer" on. Phones need rules for the four actions, which Wiggins seeds as run, except `DISMISS_ALARM`, which asks; a phone without them blocks the actions (`unmatched`), so the stage leaves these requests to the hub as before. WAGGLE.md doesn't change: these are ordinary intents with existing extra types.

"Open X" without the word "app" is taken only if the phone has an app that X names, so "open the garage door" still reaches Home Assistant. The stage can't ask the phone while matching, so when a phone that shares `apps.list` announces its capabilities, the plugin fetches its app labels in the background (at most every 30 minutes, kept by HiveMind client) and the stage checks those. A match waits up to 1.5 s for a fetch that's under way. With no list, only "open the X app" is taken.

A request said without everything it needs ("set a timer", "wake me up", "text Mom") matches with `missing` in its data. Its handler asks for the rest ("For how long?", "For what time?", "What should the message say?"), reads the answer in the stage (an alarm time is read together with the request, so "wake me up" … "7" is 7 a.m.), asks once more if it can't, and then runs the request. The rule check judges such a request with a stand-in for the missing value.

Times and durations are computed in the **target phone's** timezone (from its `waggle.capabilities`; the hub's if it announced none), not the hub's. The phone receives plain numbers. Times the phone returns are UTC and are spoken in the phone's timezone.

v1 ships English (`en-us`) locale files only.

## Request/response flow

Each request goes back to the client that asked, and the outcome is reported only after the phone confirms.

1. **Capability check:** the capability cache holds each phone's latest `waggle.capabilities`, keyed by its HiveMind client rather than its connection, which changes on every reconnect. It's kept in a file (`capabilities_file`), rewritten whenever a phone's capabilities change, so a hub restart doesn't forget a phone that stays connected and doesn't announce again. A phone known only from the file has its app list fetched on its first utterance. The handler runs the WAGGLE.md rule matcher again (a request may come from somewhere other than the pipeline stage). If the intent would be blocked, or the query isn't enabled, it reports that and stops.
2. **Lookups:** requests that need phone data (a contact's number, an app's package) send `waggle.query` first and build the intent from the result. A name that matches exactly wins; otherwise a single match is used, and several lead to a "which one?" follow-up (see below) naming at most four. Matches with the same name can't be told apart by voice, so the first is used. A contact's number is chosen the same way, by type ("cell or home"), with the last four digits when two share a type; a text goes to the contact's only mobile number without asking. The intent is checked against the phone's rules again once it's built, since a rule may name the package.
3. **Send:** the handler emits `waggle.intent` with a new request `id` and a short `description`, as a reply to the original message, so HiveMind's message context routes it to the same peer. HiveMind delivers a bus message to every client whose peer is in its `destination`. ovos-core's match message is already a reply to the utterance (its `destination` is the peer), so replying to it again would go back to `skills`; the handler first turns the request message into a stand-in for the phone's own message (`source` the peer), so replies, spoken outcomes included, reach the phone either way.
4. **Wait:** for the response with the matching `id`. The wait is `response_timeout_s` for `run` rules and `ask_timeout_s` from the phone's capabilities plus `ask_margin_s` for `ask` rules.
5. **Report:** an outcome per result, spoken as a dialog when `speak` is true and always returned in `waggle:request.response`. Success gets the confirmation; `declined` gets "Okay, cancelled"; `blocked`, `no_handler`, `launch_failed` and `permission_denied` each get a plain-words explanation; a phone-side `timeout` gets "You didn't confirm on your phone"; no response at all gets "I couldn't reach your phone."

The package never sends Waggle messages to a peer that hasn't announced capabilities, and it refuses peers whose announced `version` it doesn't support.

**Follow-up questions** ("Which Sam: Sam Lee or Sam Ortiz?") use OVOS's `get_response`, which asks the client to reopen the mic. Wiggins handles that by listening again. A request made by the persona asks its follow-up the same way. `get_response` speaks by forwarding the message it's given, so the handler hands it a message addressed to the phone; it also marks the session as waiting for this skill, and the phone sends that session back with its answer, which ovos-core's converse stage then hands to the waiting handler. The converse stage only considers the session's converse handlers, so the question's session also names this skill as one: a match of the stage already made it one, a persona's tool call didn't. An answer is read with ordinals ("the second one"), names or parts of names ("Ortiz"), and number types with their synonyms ("mobile", "office"). "Cancel" or no answer within `get_response`'s timeout ends the request with "Okay, cancelled"; an answer it can't read gets one "Sorry, which one?" before giving up.

## The persona (S4)

Requests the stage can't parse, such as compound ones ("text Jenny I'm running late, then set a timer for the oven"), fall through to the hub's LLM persona. The persona can hand them back as requests through tool use, rather than rewriting the utterance and injecting it again, which risks loops and double handling.

The package ships an `opm.agents.toolbox` plugin, `ovos-waggle-tools`, with one tool per request in the table above: `set_alarm`, `set_timer`, `show_alarms`, `get_calendar_events`, `open_app`, `call_contact`, `compose_text_message`, `stop_timers`, `dismiss_alarm` (`hour` and `minute`, a `label`, `all`, or nothing for the next alarm; a mix gets a `bad_request` result saying what to give) and `snooze_alarm` (optional `minutes`). The hub's persona uses it through ovos-agentic-loop's `ovos-native-toolcall-loop`, a solver that offers a model (its "brain", here the hub's Claude chat engine) the tools of its configured toolboxes and runs the calls it makes until it answers. Nothing upstream changes; the hub's persona config names the loop as its solver and `ovos-waggle-tools` among its toolboxes.

- **Which phone asked.** The loop calls a tool with its arguments only: no message, no session. ovos-persona runs the whole loop on the thread of its handler, which has the triggering message as an argument, so the toolbox finds it with `dig_for_message`, as `get_response` does. Its peer is the phone, and replies to it reach that phone. ovos-persona also loads toolboxes without a bus, so the tools reach the request handlers through the Waggle plugin running in the same ovos-core process.
- **What the model is offered.** The loop asks the toolbox for its tools each time it calls the model, on the same thread, so the toolbox lists only the requests the asking phone would allow, by the stage's own check (enabled here, the phone's rules, its shared queries). A satellite that isn't a Waggle phone gets no phone tools.
- A tool call runs as a `waggle:request` with `speak: false`. Its `waggle:request.response` (`ok`, `summary`, `error`) goes back to the model as the tool result, and the model says what actually happened. An `ask` rule's "Confirm on your phone." and any "which one?" question are spoken to the phone while the tool runs.
- The phone's rules and ask card apply exactly as for any other request, so the model can never do more than the phone allows.
- The tools only work in the ovos-core process that runs the Waggle plugin; a persona served elsewhere (ovos-persona-server for Home Assistant) has no phone to act on.

## Hub setup

- hivemind-core must let the phone's client key send `waggle.capabilities`, `waggle.intent.response` and `waggle.query.response` (its allowed message types), one `allow-msg` per type. The install docs will list the exact commands.
- The hub needs ovos-core 3.x (tested with 3.7.4a1, ovos-plugin-manager 2.12.5a1, ovos-workshop 9.8.16a1, ovos-bus-client 2.11.25a1, ovos-date-parser 0.34.1a1, ovos-persona 0.9.0a28 and ovos-agentic-loop 0.2.3a3). Install the package into ovos-core's environment (`pip install ovos-skill-waggle`, or the git URL), which registers the `opm.pipeline` entry point `ovos-waggle-pipeline-plugin` and the `opm.agents.toolbox` entry point `ovos-waggle-tools`; nothing goes in the skills directory.
- Add `"ovos-waggle-pipeline-plugin-high"` to `intents.pipeline` in the hub's `mycroft.conf`, right after `"ovos-converse-pipeline-plugin"` (see "The pipeline stage"), and restart ovos-core. Settings go in `intents` → `ovos-waggle-pipeline-plugin` (see "Fallbacks and configuration"). Sessions carry their pipeline, and a client that sends one back overrides the hub's: Wiggins echoes the session it was given, so a session started before the change keeps the old pipeline until it ends (30 minutes idle, or Clear conversation).
- The capability cache is written to `capabilities_file`, by default `~/.local/share/ovos-skill-waggle/capabilities.json` in ovos-core's environment. Put that directory on a persistent volume, or the phones are forgotten on every restart, as before. It's one small file written only when a phone's capabilities change.
- For the persona's tools, install ovos-agentic-loop next to ovos-persona and make the persona's solver `ovos-native-toolcall-loop` with `"toolboxes": ["ovos-waggle-tools"]` and the hub's chat engine as its brain. With ovos-plugin-manager 2.12.5a1 and ovos-agentic-loop 0.2.3a3 the loop can't load its brain from its `brain` setting (it passes `load_chat_plugin` a `config` argument that function doesn't take), so the hub has to give the loop its brain itself, e.g. with a small subclass whose `_load_brain` returns the chat engine.
- The package otherwise only listens on and emits to the OVOS bus.

## Fallbacks and configuration

- **Request from another satellite** ("set an alarm on my phone" said to a kitchen speaker): Waggle never sees it (see "Who it answers"), so it goes to the hub's own skills like any other request. Sending a phone requests it didn't make is out of scope.
- **Phone blocks the action:** for a request spoken to the phone, the stage doesn't match, so the hub's own skills handle it (see "The pipeline stage"). For a request from the persona, say that the phone doesn't allow it.
- **Ambiguous contact or app:** when `contacts.lookup` or `apps.list` returns several matches, the handler asks which one before building the intent.
- **Different apps on different phones:** intents are implicit (no package) unless the user configures one, so whatever app the phone has as its default answers.

Settings are the plugin's config, in the hub's `mycroft.conf` under `intents` → `ovos-waggle-pipeline-plugin` (where ovos-core's `OVOSPipelineFactory` reads a pipeline plugin's config). Per-request keys spell the request with underscores for dots, e.g. `enable_timer_set`, `package_alarm_set`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `response_timeout_s` | 5 | Wait for `run` intents and queries |
| `ask_margin_s` | 5 | Added to the phone's `ask_timeout_s` for `ask` intents |
| `enable_<request>` | true | Per-request enable flags; a disabled request is never matched, and a `waggle:request` for it is answered `blocked` without contacting the phone |
| `package_<request>` | none | Optional package override per request |
| `capabilities_file` | `~/.local/share/ovos-skill-waggle/capabilities.json` | Where the capability cache is kept (under `$XDG_DATA_HOME` if set); `false` keeps it in memory only |

## Testing

- **Library:** unit tests for message validation, the rule matcher (including the specificity and tie-break cases in WAGGLE.md) and every intent builder. The matcher cases are data, in `waggle/testdata/rule_cases.json`, so Wiggins' Kotlin matcher runs the same ones.
- **Wiggins:** `waggle-fake-hub --host <bus host>` connects to the OVOS messagebus that hivemind-core uses, waits for the phone's `waggle.capabilities`, and sends a scripted set of requests (built-in, or `--script` with a JSON list of steps), printing PASS or FAIL for each. It exits 0 only if every step passed.
- **Pipeline stage:** no match without capabilities, with an unsupported `version`, or when the phone blocks the action; a match for an `ask` rule; utterance-to-request cases for each request, including phrasings the alerts skill would otherwise take, and the stop, cancel, dismiss and snooze phrasings with those left alone (a bare "stop", other days, smoke alarms).
- **Pipeline end to end:** ovos-core's real `IntentService` on a `FakeBus` (with the utterance normalizer installed), loading the plugin from its entry point with a padacioso intent standing in for the alerts skill: an utterance from the Waggle phone reaches the fake phone; the same utterance from another satellite, or one the phone blocks, falls through. "Stop the timer" passes ovos-core's stop stage and sends the phone `DISMISS_TIMER` and `SHOW_TIMERS`; from another satellite, or from a phone without the new rules, it reaches a stand-in for the alerts skill. "Turn off my 6:30 am alarm" asks on the phone before it's dismissed.
- **Handlers:** each request with the fake phone, covering each outcome in the flow above, including timeouts, `speak: false`, queries the phone doesn't share, and the "which one?" questions (with scripted answers).
- **Follow-ups end to end:** "set a timer" on ovos-core's `IntentService` asks "For how long?" addressed to the phone, and the answer, sent back in the session the question came with, reaches the handler through the converse stage. A text message keeps its final punctuation, inline and as an answer.
- **Capability file:** phones survive a restart of the plugin, a phone's entry is rewritten only when its capabilities change, and a bad or unwritable file leaves the cache working from memory.
- **Persona tools:** the toolbox offers a phone only what it allows and nothing to other satellites, and runs each tool on the fake phone. End to end, ovos-core's `IntentService` with the real ovos-persona and `ovos-native-toolcall-loop` around a scripted model: an utterance the stage doesn't take reaches the persona, the model's tool call sets a timer on the phone, another satellite is offered no tools, and a "Which Sam?" question asked by a tool is answered through the converse stage.
- **End to end:** a local hivemind-core with the fake phone connected as a real HiveMind client, run by hand before releases.

## Milestones

1. **S1 — Library:** `waggle` message models, rule matcher, intent builders, fake phone and fake hub. WAGGLE.md frozen as v1. The `waggle:request` and `waggle:request.response` shapes fixed.
2. **S2 — Actions:** the pipeline stage with capability gating, the capability cache, and the alarm and timer requests with request/response and timeouts. Matches Wiggins M5.
3. **S3 — Queries:** the contacts, apps and calendar requests with the follow-up question.
4. **S4 — Persona tools:** the persona hands requests it can't answer back to the phone through tool use, with Waggle's toolbox in ovos-agentic-loop's tool-calling loop (see "The persona"). Also: the capability cache in a file, and text messages that keep their final punctuation. Needs ovos-core 3.x.
5. **0.7.0 — Stopping timers and alarms:** `timers.stop`, `alarm.dismiss` and `alarm.snooze` from the stage and as tools (see "Stopping timers and alarms"). No WAGGLE.md change; needs a Wiggins release with rules (and package visibility) for the four actions.

## Open questions

- [x] Does the OVOS persona/LLM fallback need a hint to hand phone requests to this skill instead of answering them itself? Yes, as tool use in the hub's Claude solver, making `waggle:request`s (see "The persona", S4). Decided 2026-10-06.
- [x] "Set a timer" also matches the stock OVOS timer skill. How should the two divide utterances? By who's speaking: a request spoken to a Waggle phone is for the phone, so the `waggle` stage takes it ahead of the alerts skill; every other satellite never reaches Waggle. Decided 2026-10-06.
- [x] The pipeline plugin API. It needs ovos-core 2.x; 1.3.1 can't load pipeline plugins, so S2 targets ovos-core 2.1.1. A plugin is an OPM `ConfidenceMatcherPipeline(bus, config)` registered under the `opm.pipeline` entry-point group; `match_high/medium/low(utterances, lang, message)` return an `IntentHandlerMatch(match_type, match_data, skill_id, utterance)` or None, and the pipeline names a tier as `<plugin id>-high|-medium|-low`. ovos-core's `IntentService` emits the match as `message.reply(match_type, {**message.data, **match_data, "utterance", "lang"})` with `skill_id` and the session in the context, after `{skill_id}.activate`. The plugin is also an `OVOSAbstractApplication`, like ovos-persona's `PersonaService`, and handles its own match type with `add_event(..., is_intent=True)`, which emits `ovos.utterance.handled`. Decided 2026-10-07.
- [x] Matching engine: keywords and patterns, with ovos-date-parser for durations and times (see "The pipeline stage"). No intent engine is needed for three requests; the domain gates keep it from taking non-requests, and the rules still decide. Decided 2026-10-07.
- [x] Can the handlers ask a follow-up with `get_response` from a bus event handler? Yes, by ovos-workshop 7.0.6's source: `get_response` finds the triggering message with `dig_for_message`, so it works in any handler running for a message, and ovos-core activates the skill on a match, which converse needs. Decided 2026-10-07; confirmed in S3 by an end-to-end test through ovos-core's converse stage. `get_response` forwards the message it's given, so it must be one addressed to the phone (see "Request/response flow").
- [x] How does a handler read the sender's HiveMind client? hivemind-core sets `context["peer"]` and `context["source"]` to the client's peer id, `<useragent>::<client id>::<client name>::<session id>`, on every message it injects; replies swap `source` but keep `peer`. The cache is keyed by the client id (the second field), which is stable across sessions, and keeps the latest full peer for addressing. Decided 2026-10-07.
- [x] Can a skill address a specific HiveMind peer, or only reply to the one that spoke? No longer needed: `default_phone_peer` and push wake-up were dropped with requests from other sources (decided 2026-10-12), so Waggle only answers the phone that spoke.
- [x] Should calendar reads stay hub-side through CalDAV when available, using `waggle.query` only as a fallback? No: calendar reads come from the phone's calendar through `calendar.next`, which fits what the app is for. If that turns out too limiting, the next step is a calendar MCP server for the persona, not CalDAV in Waggle. Decided 2026-10-12.
- [x] How does the persona's tool know which phone asked, when ovos-agentic-loop calls tools with their arguments only? From the stack: ovos-persona runs the loop on the handler thread of the `persona:query` message, so `dig_for_message` finds it. No upstream change (user's decision, 2026-10-12). If a later ovos-persona or ovos-agentic-loop runs tools on another thread, the toolbox offers no tools rather than the wrong phone's.
- [x] Offer every tool and let the rule check refuse, or only what the phone allows? Only what it allows: the loop asks the toolbox for its tools on the handler's thread every time it calls the model, so the toolbox can list them per phone without upstream changes. Decided 2026-10-12.
- [x] Drop ovos-core 2.x? Yes: `ToolBox` needs ovos-plugin-manager 2.12, and the hub runs the alphas (user's decision, 2026-10-12). Every dependency's minimum is now the alpha it's tested with.
- [x] What can Waggle do with "cancel my timer", "stop the alarm" and "snooze"? Only what `AlarmClock` offers: silence ringing timers and show the timers for the user to cancel a running one (no intent can), skip an alarm's next ring (never delete it), and snooze a ringing alarm. Dismissing an alarm asks on the phone by default; skipping only the next ring is accepted. User's decision, 2026-10-12.
