# ovos-skill-waggle — Spec

Oct 6, 2026 · @greennosedmule

## Purpose and scope

An OVOS pipeline plugin and skill that turn requests like "set an alarm for 6:30" into Waggle messages for Wiggins, the HiveMind Android client: generic Android intents for actions, named queries for reads. It waits for the phone's answer and speaks the result. Parsing and intent-building happen on the hub; the phone only executes.

The message format is the Waggle protocol, defined in [WAGGLE.md](WAGGLE.md); any HiveMind client that speaks it can use this package. Out of scope: anything the hub can do itself without the phone, such as reading a CalDAV calendar or controlling Home Assistant.

**Who it answers.** A request spoken to a phone is meant for that phone: "set a timer" said to Wiggins should ring on the phone, not on the hub. So for a phone that speaks Waggle, Waggle takes its requests ahead of every other skill, and for every other satellite it does nothing, leaving the stock skills (the alerts skill's hub timers) as they are.

## Repository layout

The repository holds three things that may later split apart:

| Path | What it is |
| --- | --- |
| `WAGGLE.md` | The protocol spec (canonical) |
| `waggle/` | Python library: message models and validation, the rule matcher, Android intent builders, a request/response client, and a fake phone and fake hub for tests |
| `ovos_skill_waggle/` | The pipeline stage (matching), the request handlers, the capability cache, dialogs and locale files, settings |

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
| `calendar.next` | optional `count` | query `calendar.next` | "Dentist, tomorrow at 2 PM." |
| `app.open` | `name` | query `apps.list` {name}, then intent `MAIN` + `LAUNCHER` with the matched package | "Opening Camera." |
| `contact.call` | `name` | query `contacts.lookup`, then intent `DIAL` with a `tel:` URI | "Dialing Mom. Tap call to connect." |
| `message.compose` | `name`, `body` | query `contacts.lookup`, then intent `SENDTO` with an `smsto:` URI and `sms_body` | "Message ready. Tap send on your phone." |

Each handler builds its Android intent with a `waggle` builder, so the handlers are mostly a library of intent builders. When it finishes, the handler emits `waggle:request.response` with `{"ok": ..., "error"?: <WAGGLE.md code or "unreachable">, "summary": <what happened, in words>}`, as a reply to the request. With `speak: true` (the pipeline's requests) it also speaks the outcome; with `speak: false` (the persona's tool calls) the caller says it in its own words.

### The pipeline stage

The stage `waggle` goes near the front of the hub's pipeline, after `stop_high` and `converse` (so a skill's follow-up question, Waggle's own included, still gets its answer) and before every stage that could take a phone request, such as `adapt_high` and the alerts skill:

```json
"pipeline": ["stop_high", "converse", "waggle", "ocp_high", "padatious_high", "adapt_high", "…"]
```

It matches only when all of these hold, and otherwise returns no match, so the rest of the pipeline runs as if Waggle weren't installed:

1. **The speaker is a Waggle phone:** the utterance came from a peer that has announced `waggle.capabilities` in a `version` this package supports. Satellites that don't speak Waggle never announce, so they're never matched, and no list of client IDs is kept on the hub.
2. **The utterance is a request it understands.** Matching is deliberately generous for phones in Waggle's domains (times, alarms, timers): a phrasing the stage misses would go on to the alerts skill and set a timer on the hub, which is the wrong place. A false match costs little, since the phone's rules and ask card still decide.
3. **The phone would allow it,** by the WAGGLE.md rule matcher run on the cached capabilities. An intent the phone blocks (by a rule, or `unmatched: block` with no rule) isn't Waggle's to take: the user chose not to allow it on the phone, so the request goes on to the stock skills and is handled on the hub. A query the phone hasn't enabled is treated the same way. An `ask` rule matches; the phone asks.

A match produces a `waggle:request` with `speak: true`.

Times and durations are parsed with ovos-utils' date and duration helpers in the **target phone's** timezone (from its `waggle.capabilities`), not the hub's. The phone receives plain numbers. Times the phone returns are UTC and are spoken in the phone's timezone.

v1 ships English (`en-us`) locale files only.

## Request/response flow

Each request goes back to the client that asked, and the outcome is reported only after the phone confirms.

1. **Capability check:** the capability cache holds each phone's latest `waggle.capabilities`, keyed by its HiveMind client rather than its connection, which changes on every reconnect. The handler runs the WAGGLE.md rule matcher again (a request may come from somewhere other than the pipeline stage). If the intent would be blocked, or the query isn't enabled, it reports that and stops.
2. **Lookups:** requests that need phone data (a contact's number, an app's package) send `waggle.query` first and build the intent from the result. Several matches lead to a "which one?" follow-up (see below).
3. **Send:** the handler emits `waggle.intent` with a new request `id` and a short `description`, as a reply to the original message, so HiveMind's message context routes it to the same peer.
4. **Wait:** for the response with the matching `id`. The wait is `response_timeout_s` for `run` rules and `ask_timeout_s` from the phone's capabilities plus `ask_margin_s` for `ask` rules.
5. **Report:** an outcome per result, spoken as a dialog when `speak` is true and always returned in `waggle:request.response`. Success gets the confirmation; `declined` gets "Okay, cancelled"; `blocked`, `no_handler`, `launch_failed` and `permission_denied` each get a plain-words explanation; a phone-side `timeout` gets "You didn't confirm on your phone"; no response at all gets "I couldn't reach your phone."

The package never sends Waggle messages to a peer that hasn't announced capabilities, and it refuses peers whose announced `version` it doesn't support.

**Follow-up questions** ("Which Sam: Sam Lee or Sam Ortiz?") use OVOS's `get_response`, which asks the client to reopen the mic. Wiggins handles that by listening again. A request made by the persona asks its follow-up the same way.

## The persona (S4)

Requests the stage can't parse, such as compound ones ("text Jenny I'm running late, then set a timer for the oven"), fall through to the hub's LLM persona. The persona can hand them back as requests through tool use, rather than rewriting the utterance and injecting it again, which risks loops and double handling:

- The hub's Claude solver (a custom OVOS solver for Claude) offers Claude one tool per request in the table above, built from that phone's capabilities, so it's only offered what the phone allows.
- A tool call becomes a `waggle:request` with `speak: false`. The `waggle:request.response` goes back to Claude as the tool result, and Claude says what actually happened.
- The phone's rules and ask card apply exactly as for any other request, so the LLM can never do more than the phone allows.
- **Needed first:** the solver interface (`stream_chat_utterances(messages, lang, units)`) doesn't see the bus message, so it doesn't know which phone asked and has no bus to send on. ovos-persona has to pass the message context through to the solver; the hub already runs ovos-persona 0.7.1 over the image's version, so this can be a small patch, offered upstream.
- **Until then,** the persona's system prompt says it can't set alarms or timers or do anything on the phone, so it never claims to have done so.

## Hub setup

- hivemind-core must let the phone's client key send `waggle.capabilities`, `waggle.intent.response` and `waggle.query.response` (its allowed message types), one `allow-msg` per type. The install docs will list the exact commands.
- The `waggle` stage goes into the pipeline in the hub's `mycroft.conf` (`intents.pipeline`), after `converse`. Sessions carry their pipeline, and a client that sends one back overrides the hub's: Wiggins echoes the session it was given, so a session started before the change keeps the old pipeline until it ends (30 minutes idle, or Clear conversation).
- The package otherwise only listens on and emits to the OVOS bus.

## Fallbacks and configuration

- **Request from a non-phone satellite** ("set an alarm on my phone" said to a kitchen Pi, S4): send to a configured default phone peer if one is set and online; otherwise say the phone isn't connected. Times are computed in the phone's timezone, not the Pi's. Never fall back silently to a hub-side alarm or timer, since the user asked for the phone.
- **Phone blocks the action:** for a request spoken to the phone, the stage doesn't match, so the hub's own skills handle it (see "The pipeline stage"). For a request from elsewhere (the persona, a default phone peer), say that the phone doesn't allow it.
- **Ambiguous contact or app:** when `contacts.lookup` or `apps.list` returns several matches, the handler asks which one before building the intent.
- **Different apps on different phones:** intents are implicit (no package) unless the user configures one, so whatever app the phone has as its default answers.
- **Phone connected only while its UI is open:** Wiggins is usually connected only during an interaction, so "default phone peer" requests mostly fail with "your phone isn't connected" until push wake-up exists (see Milestones).

Settings (`settings.json`):

| Setting | Default | Purpose |
| --- | --- | --- |
| `response_timeout_s` | 5 | Wait for `run` intents and queries |
| `ask_margin_s` | 5 | Added to the phone's `ask_timeout_s` for `ask` intents |
| `default_phone_peer` | none | HiveMind client to use when a non-phone satellite asks (S4) |
| `enable_<request>` | true | Per-request enable flags; a disabled request is never matched or handled |
| `package_<request>` | none | Optional package override per request |

## Testing

- **Library:** unit tests for message validation, the rule matcher (including the specificity and tie-break cases in WAGGLE.md) and every intent builder. The matcher cases are data, in `waggle/testdata/rule_cases.json`, so Wiggins' Kotlin matcher runs the same ones.
- **Wiggins:** `waggle-fake-hub --host <bus host>` connects to the OVOS messagebus that hivemind-core uses, waits for the phone's `waggle.capabilities`, and sends a scripted set of requests (built-in, or `--script` with a JSON list of steps), printing PASS or FAIL for each. It exits 0 only if every step passed.
- **Pipeline stage:** no match without capabilities, with an unsupported `version`, or when the phone blocks the action; a match for an `ask` rule; utterance-to-request cases for each request, including phrasings the alerts skill would otherwise take.
- **Handlers:** each request with the fake phone, covering each outcome in the flow above, including timeouts, `speak: false`, and the follow-up question.
- **End to end:** a local hivemind-core with the fake phone connected as a real HiveMind client, run by hand before releases.

## Milestones

1. **S1 — Library:** `waggle` message models, rule matcher, intent builders, fake phone and fake hub. WAGGLE.md frozen as v1. The `waggle:request` and `waggle:request.response` shapes fixed.
2. **S2 — Actions:** the pipeline stage with capability gating, the capability cache, and the alarm and timer requests with request/response and timeouts. Matches Wiggins M5.
3. **S3 — Queries:** the contacts, apps and calendar requests with the follow-up question.
4. **S4 — Reach:** `default_phone_peer`, the persona's tools (with the ovos-persona context patch), and UnifiedPush wake-up once Wiggins supports it.

## Open questions

- [x] Does the OVOS persona/LLM fallback need a hint to hand phone requests to this skill instead of answering them itself? Yes, as tool use in the hub's Claude solver, making `waggle:request`s (see "The persona", S4). Decided 2026-10-06.
- [x] "Set a timer" also matches the stock OVOS timer skill. How should the two divide utterances? By who's speaking: a request spoken to a Waggle phone is for the phone, so the `waggle` stage takes it ahead of the alerts skill; every other satellite never reaches Waggle. Decided 2026-10-06.
- [ ] The pipeline plugin API in the hub's ovos-core (1.3.1): how a stage returns a match, how its `match_type` reaches the handler, and the stage name it registers. The hub's `ovos-persona-pipeline-plugin-low` stage is a working example.
- [ ] Matching engine for the stage: Padacioso (as OCP and the persona use) or keywords, and how generous it can be without taking non-requests.
- [ ] Can the handlers ask a follow-up with `get_response` from a bus event handler rather than an intent handler?
- [ ] How does a handler read the sender's HiveMind client from a message's context, to key the capability cache? HiveMind's peer id has the form `<useragent>::<client id>::<client name>::<session id>` (Wiggins' `docs/hivemind-protocol.md` §11.2); the client id (from `add-client`) is stable across reconnects, the session id isn't.
- [ ] Can a skill address a specific HiveMind peer (for `default_phone_peer`), or only reply to the one that spoke? Ask in the HiveMind Matrix room.
- [ ] Should calendar reads stay hub-side through CalDAV when available, using `waggle.query` only as a fallback? The `calendar.next` result shape is iCalendar-based so either source fits.
