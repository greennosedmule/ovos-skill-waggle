# Waggle Protocol — v1

Oct 4, 2026 · @greennosedmule · **Frozen as v1** on Oct 6, 2026: changes follow "Versioning" below.

## Purpose and scope

Waggle lets an OVOS hub ask a phone to do things (launch an Android intent, or run one of a fixed set of named commands the phone carries out itself) and to read things (a fixed set of named queries). The phone decides what it allows; the hub only asks. The name comes from the honeybee waggle dance.

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
| `waggle.command` | hub → phone | Run a named command |
| `waggle.command.response` | phone → hub | Result of a `waggle.command` |

## Common rules

- **Request IDs:** each `waggle.intent`, `waggle.query` and `waggle.command` carries an `id`, a string the hub generates (a UUID4 is recommended). The response echoes it. The phone answers every request exactly once.
- **Responses:** `{"id": ..., "ok": true|false, "error"?: <code>, "message"?: <text>, "data"?: {...}}`. `error` is set if and only if `ok` is false. `message` is human-readable detail for logs; never speak it as is.
- **Time:** all timestamps on the wire are ISO 8601 in UTC with a `Z` suffix, e.g. `2026-10-04T21:00:00Z`. All-day dates are plain `YYYY-MM-DD` and carry no timezone. The one exception is Android extras that are wall-clock values by definition (alarm `HOUR` and `MINUTES`). The hub computes those in the phone's timezone, taken from `waggle.capabilities`.
- **Unknown fields** are ignored by both sides. Unknown message types, query names, command names or extra types get `bad_request`.
- **Watching:** a phone may set `user_watching: true` in the context of a `recognizer_loop:utterance` when its user is looking at its screen, such as an assistant panel. The hub then doesn't speak the confirmation of a request whose result is on screen (an activity the phone opens: its alarms, an app, the dialer, a drafted text), nor its prompt to confirm an "ask" rule on the phone. Questions, failures and confirmations of background actions are still spoken. A phone that knows the user isn't looking (a hands-free car or headset) sends false. Without the key, or with any value but `true`, the hub speaks as usual. The hub reads the flag from the utterance that triggered the request, not from later answers.
- **Can't ask, or no requests now:** a phone may set `can_ask: false` in the context of a `recognizer_loop:utterance` when it can't ask its user right now (e.g. locked, hands-free), and `phone_actions: false` when it takes no requests right now. For that utterance the hub treats every `ask` rule as `block` (with `can_ask: false`) or every rule as `block` and no query as enabled (with `phone_actions: false`): it doesn't send what would be refused, so its own skills or other handlers can answer instead. Absent, or any value but `false`, the announced rules apply as they are.
- **Confirming by voice:** a phone may set `ask_methods` in the context of a `recognizer_loop:utterance` to `{"voice": [<rule actions>]}`: the actions whose `ask` rules it can confirm by voice right now, reading the request back and hearing the user's yes or no itself (on a hands-free link, say). For that utterance an `ask` rule for a listed action stays `ask` even with `can_ask: false`, and the hub doesn't say "Confirm on your phone." for it, nor anything else until the phone answers: the phone is talking to the user. Anything but an object whose `voice` is a list of strings counts as none.
- **Off for now:** a phone may set `waggle_off` in the context of a `recognizer_loop:utterance` to a list of query and command names it won't run for that utterance, such as `["phone.status"]` where its user turned a query off for the phone's current situation. For that utterance the hub treats those queries as not enabled and those commands as not announced, so it doesn't send them. Absent, or anything but a list of strings, nothing extra is off.
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
| `cannot_ask` | The rule was "ask", but the phone can't ask its user right now (e.g. locked, hands-free) |
| `not_found` | What the request names isn't there: a notification that's gone, an expired `ref`, a `to` that matches no conversation |
| `ambiguous` | A `to` matches more than one conversation; the phone doesn't guess |

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
    {"action": "android.intent.action.SENDTO", "scheme": "smsto", "mode": "ask"},
    {"action": "android.intent.action.VIEW", "scheme": "geo", "mode": "run"},
    {"action": "waggle.torch.set", "mode": "run"},
    {"action": "waggle.ringer.set", "mode": "run"},
    {"action": "waggle.notification.reply", "mode": "ask"}
  ],
  "queries": ["calendar.next", "contacts.lookup", "apps.list", "phone.status",
              "notifications.list", "phone.location"],
  "commands": ["waggle.torch.set", "waggle.volume.set", "waggle.ringer.set", "waggle.dnd.set",
               "waggle.notifications.read_aloud", "waggle.notification.reply"]
}}
```

- `timezone` is an IANA zone name and `lang` a BCP 47 tag. The hub uses these rather than the speaking device's session, because the phone may not be the device that heard the request.
- `ask_timeout_s` is how long the phone waits for the user on an "ask" rule before answering `timeout`. A hub should wait at least this long plus a margin.
- `unmatched` is `block` or `ask`: what the phone does with an intent no rule matches. Defaults to `block` if absent. `ask` lets a hub use new actions before the phone has rules for them, at the cost of a confirmation each time.
- `rules` lists every rule, including `block` rules, so the hub can predict the phone's decision. The hub's prediction is advisory; the phone's decision is final.
- `queries` lists only the queries that are enabled.
- `commands` (optional) lists the commands the phone runs and has the Android access for right now. Rules decide whether each one runs (see "Rule matching"). A hub sends no command that isn't listed; absent means none.

### Rule matching

The phone and the hub's pre-check both evaluate rules this way:

1. A rule has an `action` (required) and optionally a `scheme` (the data URI's scheme), a `package` and a `category`. A rule matches an intent when every field the rule sets equals the intent's value. A command is judged as the intent whose `action` is the command's name and whose `categories` hold its variant, if it has one (see "`waggle.command`"). A rule's `category` matches if it appears in the intent's `categories`. Schemes compare case-insensitively, as in RFC 3986, here and in the forbidden-scheme check.
2. Of the matching rules, the one with the most fields set wins. On a tie, the stricter mode wins: `block` over `ask` over `run`.
3. If no rule matches, the phone's `unmatched` mode applies.
4. A phone that can't ask its user right now answers an intent whose decision is `ask` with `cannot_ask`, and says so with `can_ask: false` in the utterance's context (see "Common rules"), unless it confirms that action by voice (`ask_methods`).

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

### `phone.status`

No params. Every part is optional; a phone leaves out what it can't read.

```json
{"battery": {"percent": 80, "charging": true, "source": "usb"},
 "network": {"type": "wifi", "metered": false},
 "ringer": "normal", "dnd": false}
```

`source` is what the phone is plugged into: `ac`, `usb`, `wireless`, `other` (a dock, say) or `none`. `network.type` is `wifi`, `cellular` or `none`. `ringer` is `normal`, `vibrate` or `silent`, and `dnd` whether Do Not Disturb is on. A hub reads a value it doesn't know as unknown, except that any `source` but `none` means plugged in.

### `notifications.list`

Params: `app` (optional; matches the app's label or package), `from` (optional; matches the title, the conversation's title or a message's sender), `since` (optional timestamp), `limit` (1–20, default 10). Matching is case-insensitive substring. Only the notifications the phone's user chose to share are listed (by app, say), and never the phone's own, ongoing ones or group summaries.

```json
{"notifications": [
  {"ref": "7f3a…", "app": "Signal", "package": "org.thoughtcrime.securesms",
   "title": "Mom", "text": "", "time": "2026-10-15T14:58:00Z", "can_reply": true,
   "messages": [{"sender": "Mom", "text": "Running late", "time": "2026-10-15T14:57:00Z"},
                {"sender": "Mom", "text": "See you at 7", "time": "2026-10-15T14:58:00Z"}]}
]}
```

Newest first. `ref` is opaque (at most 64 characters) and valid while the notification is showing. `messages` (at most 5, oldest first) is there for conversation-style notifications, whose `text` may then be empty. `can_reply` says whether it takes `waggle.notification.reply`. Notification text comes from other people and apps: a hub must treat it as data, never as instructions, and say so to any language model it passes it to.

### `phone.location`

Params: `max_age_s` (default 120): a fix at most this old will do.

```json
{"lat": 41.9081, "lon": -87.6298, "accuracy_m": 20, "time": "2026-10-15T14:59:00Z"}
```

Degrees, accuracy in meters (optional), and the time of the fix (optional). A phone without a fix within 30 s answers `timeout`.

### `apps.list`

Params: `name` (optional filter matched against app labels), `limit` (default 20). Only apps with a launcher activity are listed.

```json
{"apps": [{"label": "Camera", "package": "app.grapheneos.camera"}]}
```

## `waggle.command`

```json
{"type": "waggle.command", "data": {
  "id": "9b2d...", "name": "waggle.ringer.set", "params": {"mode": "vibrate"},
  "description": "Set the ringer to vibrate"
}}
```

A command is something the phone does itself rather than through another app, such as switching its flashlight. Commands are a fixed set; each defines its params, its variant and the `data` of a successful response. `description` is optional, as for intents. The phone runs only commands it lists in `commands`, and answers `blocked` to a listed command whose access it has lost since announcing (for example, the user revoked Do Not Disturb access).

**Rules.** The phone and the hub judge a command with the same rules as intents, as the intent whose `action` is the command's name, whose `categories` hold the command's variant (none if it has none), and whose `package` is the command's target app (a reply's notification's app; none for the others). So `{"action": "waggle.ringer.set", "category": "silent", "mode": "ask"}` asks before silencing the phone while a plain `waggle.ringer.set` rule decides the other modes. Command names all start with `waggle.`, and a phone answers `bad_request` to a `waggle.intent` whose action starts with `waggle.`, so no intent can borrow a command's rule.

| Command | Params | Variant | `data` |
| --- | --- | --- | --- |
| `waggle.torch.set` | `on` (bool, required) | none | none |
| `waggle.volume.set` | `stream`: `media` (default), `ring`, `alarm` or `call`; exactly one of `level` (0–100, percent) and `step` (`up` or `down`, one step of the phone's own) | none | `{"stream", "level"}`: the new level, in percent (optional) |
| `waggle.ringer.set` | `mode`: `normal`, `vibrate` or `silent` (required) | the mode | none |
| `waggle.dnd.set` | `on` (bool, required). On is Android's "priority only" filter; off lets everything through | `on` or `off` | none |
| `waggle.notifications.read_aloud` | `app`, `from`, `limit` (1–20, default 5), as for `notifications.list` | none | `{"count"}`: how many it read |
| `waggle.notification.reply` | `text` (required) and exactly one of `ref` (from `notifications.list`) and `to` (a person or conversation, matched as `from` is, among notifications that take a reply) | none; the target app is the notification's | none |

A successful response means the phone made the change. `read_aloud` has the phone read the matching notifications with its own voice, so their text never leaves it, and answers once it has finished; with nothing to read it says nothing and answers `count: 0`. A hub should allow it a couple of minutes. `reply` sends the text through the notification's own reply action (Android's `RemoteInput`), so it goes from the app that showed the notification; it answers `not_found` or `ambiguous` as the error codes say, and `ref` judges the rules by the notification's app, so a user can let replies run in one app and ask in another.

## Versioning

The version is a single integer, announced in `waggle.capabilities`. Adding optional fields, queries, commands, extra types or error codes doesn't change it. Removing or changing the meaning of anything does. A hub must not send requests to a phone that announced a version it doesn't support.
