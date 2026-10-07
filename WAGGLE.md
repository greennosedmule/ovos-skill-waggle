# Waggle Protocol — v1

Oct 4, 2026 · @greennosedmule · **Frozen as v1** on Oct 6, 2026: changes follow "Versioning" below.

## Purpose and scope

Waggle lets an OVOS hub ask a phone to do things (launch an Android intent) and to read things (a fixed set of named queries). The phone decides what it allows; the hub only asks. The name comes from the honeybee waggle dance.

Waggle messages are ordinary OVOS bus messages. Over HiveMind they travel like any other bus message between the hub and one satellite. This document is the canonical definition. The reference implementations are the `waggle` Python library and skill in this repository, and the Wiggins Android client.

## Roles

- **Hub:** sends requests. In practice this is ovos-skill-waggle, or any skill using the `waggle` library.
- **Phone:** a HiveMind client that runs requests within limits its user sets. It announces those limits as capabilities.

| Message | Direction | Purpose |
| --- | --- | --- |
| `waggle.capabilities` | phone → hub | What the phone allows; sent on connect and whenever settings change |
| `waggle.intent` | hub → phone | Launch an Android activity intent |
| `waggle.intent.response` | phone → hub | Result of a `waggle.intent` |
| `waggle.query` | hub → phone | Run a named read |
| `waggle.query.response` | phone → hub | Result of a `waggle.query` |

## Common rules

- **Request IDs:** each `waggle.intent` and `waggle.query` carries an `id`, a string the hub generates (a UUID4 is recommended). The response echoes it. The phone answers every request exactly once.
- **Responses:** `{"id": ..., "ok": true|false, "error"?: <code>, "message"?: <text>, "data"?: {...}}`. `error` is set if and only if `ok` is false. `message` is human-readable detail for logs; never speak it as is.
- **Time:** all timestamps on the wire are ISO 8601 in UTC with a `Z` suffix, e.g. `2026-10-04T21:00:00Z`. All-day dates are plain `YYYY-MM-DD` and carry no timezone. The one exception is Android extras that are wall-clock values by definition (alarm `HOUR` and `MINUTES`). The hub computes those in the phone's timezone, taken from `waggle.capabilities`.
- **Unknown fields** are ignored by both sides. Unknown message types, query names or extra types get `bad_request`.
- **Routing:** the hub sends requests as replies to the message that triggered them (`Message.reply`), so HiveMind delivers them to the right peer. Responses come back as new messages from the phone.

## Error codes

| Code | Meaning |
| --- | --- |
| `blocked` | A phone rule blocks this intent, no rule matches it and `unmatched` is `block`, or the query isn't enabled |
| `declined` | The rule was "ask" and the user said no |
| `timeout` | The rule was "ask" and the user didn't answer in time |
| `no_handler` | No app on the phone handles the intent |
| `launch_failed` | Android refused the launch, e.g. a background activity start was blocked |
| `permission_denied` | The phone lacks the Android permission a query needs |
| `bad_request` | The request is malformed, uses a forbidden URI scheme, or names an unknown query or extra type |
| `unsupported_version` | The request uses a protocol version the phone doesn't speak |

## `waggle.capabilities`

```json
{"type": "waggle.capabilities", "data": {
  "version": 1,
  "client": {"name": "Wiggins", "version": "0.3.0"},
  "timezone": "America/Chicago",
  "lang": "en-US",
  "ask_timeout_s": 15,
  "unmatched": "block",
  "rules": [
    {"action": "android.intent.action.SET_ALARM", "mode": "run"},
    {"action": "android.intent.action.SET_TIMER", "mode": "run"},
    {"action": "android.intent.action.SHOW_ALARMS", "mode": "run"},
    {"action": "android.intent.action.MAIN", "category": "android.intent.category.LAUNCHER", "mode": "run"},
    {"action": "android.intent.action.DIAL", "scheme": "tel", "mode": "ask"},
    {"action": "android.intent.action.SENDTO", "scheme": "smsto", "mode": "ask"}
  ],
  "queries": ["calendar.next", "contacts.lookup", "apps.list"]
}}
```

- `timezone` is an IANA zone name and `lang` a BCP 47 tag. The hub uses these rather than the speaking device's session, because the phone may not be the device that heard the request.
- `ask_timeout_s` is how long the phone waits for the user on an "ask" rule before answering `timeout`. A hub should wait at least this long plus a margin.
- `unmatched` is `block` or `ask`: what the phone does with an intent no rule matches. Defaults to `block` if absent. `ask` lets a hub use new actions before the phone has rules for them, at the cost of a confirmation each time.
- `rules` lists every rule, including `block` rules, so the hub can predict the phone's decision. The hub's prediction is advisory; the phone's decision is final.
- `queries` lists only the queries that are enabled.

### Rule matching

The phone and the hub's pre-check both evaluate rules this way:

1. A rule has an `action` (required) and optionally a `scheme` (the data URI's scheme), a `package` and a `category`. A rule matches an intent when every field the rule sets equals the intent's value. A rule's `category` matches if it appears in the intent's `categories`. Schemes compare case-insensitively, as in RFC 3986, here and in the forbidden-scheme check.
2. Of the matching rules, the one with the most fields set wins. On a tie, the stricter mode wins: `block` over `ask` over `run`.
3. If no rule matches, the phone's `unmatched` mode applies.

## `waggle.intent`

```json
{"type": "waggle.intent", "data": {
  "id": "5f0c1e9a-3b7d-4c4e-9a51-2f8e6b0d7c11",
  "description": "Set an alarm for 6:30 AM",
  "action": "android.intent.action.SET_ALARM",
  "extras": {
    "android.intent.extra.alarm.HOUR": {"type": "int", "value": 6},
    "android.intent.extra.alarm.MINUTES": {"type": "int", "value": 30},
    "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": true}
  }
}}
```

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | Request ID |
| `action` | yes | Intent action string |
| `description` | no | Short human text; the phone shows it next to the raw intent when asking the user |
| `data` | no | Data URI. Schemes `content`, `file`, `intent` and `android-app` are rejected with `bad_request` |
| `mime_type` | no | Intent MIME type |
| `categories` | no | List of category strings |
| `package` | no | Restricts resolution to one app. There is no component field: explicit components aren't part of Waggle |
| `extras` | no | Map of extra name to `{"type", "value"}` |

Extra types: `int`, `long`, `float`, `double`, `bool`, `string`, `string[]`, `uri`. A `uri` extra follows the same scheme rules as `data`.

The hub can't set intent flags. The phone launches with only the flags it needs itself (such as `FLAG_ACTIVITY_NEW_TASK`) and never grants URI permissions. A successful response means Android accepted the launch, not that the target app did what was asked.

## `waggle.query`

```json
{"type": "waggle.query", "data": {
  "id": "c2a1...", "name": "contacts.lookup", "params": {"name": "Mom"}
}}
```

Queries are a fixed set; each defines its params and the `data` of a successful response. Field names follow iCalendar and vCard where those have a name for the field, so a hub-side CalDAV or CardDAV source can produce the same shapes.

### `calendar.next`

Params: `count` (1–10, default 1), `within_days` (default 7).

```json
{"events": [
  {"summary": "Dentist", "dtstart": "2026-10-05T19:00:00Z", "dtend": "2026-10-05T20:00:00Z",
   "all_day": false, "location": "123 Main St", "calendar": "Personal"}
]}
```

All-day events use date-only `dtstart`/`dtend`, with `dtend` exclusive as in iCalendar. Events come back in start order, starting from now.

### `contacts.lookup`

Params: `name` (required), `limit` (default 5). The phone does the name matching (display name and nickname) and returns only the matches, never the whole address book.

```json
{"contacts": [
  {"fn": "Mom", "tel": [{"value": "+15551234567", "type": "cell"}]}
]}
```

`type` is one of `cell`, `home`, `work` or `other`, mapped from Android's phone types.

### `apps.list`

Params: `name` (optional filter matched against app labels), `limit` (default 20). Only apps with a launcher activity are listed.

```json
{"apps": [{"label": "Camera", "package": "app.grapheneos.camera"}]}
```

## Versioning

The version is a single integer, announced in `waggle.capabilities`. Adding optional fields, queries, extra types or error codes doesn't change it. Removing or changing the meaning of anything does. A hub must not send requests to a phone that announced a version it doesn't support.

## Reserved for later

- **Push wake-up:** a hub may wake a disconnected phone through UnifiedPush so it connects and receives pending requests. The payload and the pending-request queue will be specified when Wiggins implements it.
