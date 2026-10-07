"""Utterance to request: the language half of the pipeline stage (see SPEC.md "The pipeline stage").

Matching is a keyword gate per domain followed by number and time parsing,
deliberately generous for phones in Waggle's domains: a phrasing missed here
goes on to the hub's alerts skill and sets the alarm or timer on the hub,
which is the wrong place, while a false match costs little, since the phone's
rules and ask card still decide.

Durations go through ovos-date-parser's ``extract_duration`` after rewriting
the common phrasings it misses ("an hour and a half", "a minute", "10 mins").
Clock times are read by the patterns below, with ovos-date-parser's
``extract_datetime`` as a last resort; in its 0.29 release it misreads
"18:30", "half past six" and "seven fifteen pm". Everything is computed
against ``now`` in the phone's timezone. Alarms carry only a wall-clock hour
and minute (Android's ``SET_ALARM`` has no date and rings at the next
matching time), so "tomorrow" only steers a.m. versus p.m., and an alarm for
another day ("Friday at 7", "every weekday") isn't taken.

English only (v1); other languages never match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from ovos_date_parser import extract_datetime, extract_duration
from ovos_number_parser import numbers_to_digits
from ovos_utils.log import LOG

from ovos_skill_waggle.requests import (
    ALARM_SET, ALARMS_SHOW, APP_OPEN, CALENDAR_NEXT, CONTACT_CALL, MESSAGE_COMPOSE, TIMER_SET,
    WaggleRequest,
)
from waggle.intents import TIMER_MAX_S, TIMER_MIN_S

PARSE_LANG = "en-us"
_TOKEN = "@@"  # where extract_duration found the duration
# numbers_to_digits turns "half" and "quarter" into 0.5 and 0.25, which wrecks
# "half past six"; they're hidden behind these while numbers are converted.
_HALF, _QUARTER = "xhalfx", "xquarterx"

_WS = re.compile(r"\s+")


def _sub(pattern: str, repl, text: str) -> str:
    return re.sub(pattern, repl, text)


# --- normalization -----------------------------------------------------------

_FILLERS = re.compile(
    r"^(?:(?:hey|hi|ok|okay|please|so|um|uh|now|then|"
    r"(?:can|could|would|will) you(?: please)?|i (?:want|need|would like|'d like) (?:you )?to|"
    r"go ahead and|just)\s+)+")


def normalize(utterance: str) -> str:
    """Lowercase, plain words: "6:30 A.M." -> "6:30 am", "10-minute" -> "10 minute"."""
    text = utterance.lower().replace("’", "'")
    text = _sub(r"\b([ap])\s*\.\s*m\b\.?", r"\1m", text)        # a.m., p. m.
    text = _sub(r"\b([ap])\s+m\b", r"\1m", text)                 # "a m" from STT
    text = _sub(r"\bo\s*'?\s*clock\b", "oclock", text)
    text = _sub(r"(?<=\d)\s*(am|pm)\b", r" \1", text)            # 7pm -> 7 pm
    text = text.replace("-", " ")
    text = _sub(r"[,!?;:\"](?!\d)", " ", text)                   # keep 6:30
    text = _sub(r"(?<!\d)\.|\.(?!\d)", " ", text)               # keep 1.5 and 6.30
    text = _WS.sub(" ", text).strip()
    text = _FILLERS.sub("", text)
    text = _sub(r"\s*\bplease$", "", text)
    text = _sub(r"\b(?:on|to|in) (?:my|the) (?:phone|cell(?: phone)?|mobile)\b", " ", text)
    return _WS.sub(" ", text).strip()


def digitize(text: str) -> str:
    """Number words to digits, keeping "half" and "quarter" as words."""
    text = _sub(r"\bhalf\b", _HALF, text)
    text = _sub(r"\bquarters?\b", _QUARTER, text)
    text = _sub(r"\ba couple(?: of)?\b", "2", text)
    try:
        text = numbers_to_digits(text, PARSE_LANG)
    except Exception as e:  # a parser bug mustn't take the stage down
        LOG.debug(f"numbers_to_digits failed on {text!r}: {e}")
    return _WS.sub(" ", text).strip()


# --- durations ---------------------------------------------------------------

_UNIT_SECONDS = {"hour": 3600, "minute": 60, "second": 1}


def _half_more(m: re.Match) -> str:
    count = 1.0 if m.group(1) in ("a", "an") else float(m.group(1))
    unit = m.group(2)
    return f"{round((count + 0.5) * _UNIT_SECONDS[unit])} seconds"


def _rewrite_durations(text: str) -> str:
    """Rewrite the duration phrasings extract_duration misses into ones it reads."""
    text = _sub(r"\b(?:mins?)\b", "minutes", text)
    text = _sub(r"\b(?:hrs?)\b", "hours", text)
    text = _sub(r"\b(?:secs?)\b", "seconds", text)
    # "an hour and a half", "2 minutes and a half"
    text = _sub(rf"\b(\d+(?:\.\d+)?|an?) (hour|minute|second)s? and a {_HALF}\b", _half_more, text)
    # "1 and a half hours"
    text = _sub(rf"\b(\d+) and a {_HALF} (hour|minute|second)s?\b",
                lambda m: f"{int(m.group(1)) * _UNIT_SECONDS[m.group(2)] + _UNIT_SECONDS[m.group(2)] // 2}"
                          f" seconds", text)
    # "half an hour", "a half hour", "half a minute"
    text = _sub(rf"\b(?:an? )?{_HALF} (?:an? )?(hour|minute)\b",
                lambda m: f"{_UNIT_SECONDS[m.group(1)] // 2} seconds", text)
    # "a quarter of an hour", "quarter hour", "3 quarters of an hour"
    text = _sub(rf"\b(?:(\d+|an?) )?{_QUARTER}s? (?:of )?(?:an )?hour\b",
                lambda m: f"{15 * (int(m.group(1)) if m.group(1) and m.group(1).isdigit() else 1)}"
                          f" minutes", text)
    # "a minute", "an hour", "one second"
    text = _sub(r"\b(?:an?) (hour|minute|second)\b", r"1 \1", text)
    return text


@dataclass(frozen=True)
class Duration:
    seconds: int
    before: str  # the text before the duration
    after: str   # the text after it


def find_duration(text: str) -> Optional[Duration]:
    """The first duration in digitized ``text``, with the words around it."""
    rewritten = _rewrite_durations(text)
    try:
        delta, remainder = extract_duration(rewritten, PARSE_LANG, replace_token=_TOKEN)
    except Exception as e:
        LOG.debug(f"extract_duration failed on {rewritten!r}: {e}")
        return None
    if not isinstance(delta, timedelta) or _TOKEN not in remainder:
        return None
    seconds = round(delta.total_seconds())
    # "2 hours and 15 minutes" leaves "@@ and @@": the duration spans them all.
    remainder = _sub(rf"{_TOKEN}(?: (?:and )?{_TOKEN})+", _TOKEN, remainder)
    before, _, after = remainder.partition(_TOKEN)
    return Duration(seconds, before.strip(), after.strip())


# --- labels ------------------------------------------------------------------

_LABEL_STOP = frozenset("""
    a an the my this that new one another set start create make add begin put timer timers alarm
    alarms countdown count down for to and of in on at with please kitchen quick minute minutes
    hour hours second seconds me it phone
""".split())
_EXPLICIT_LABEL = re.compile(r"\b(?:called|named|labell?ed|label|titled)\s+(.+)$")


def _clean_label(words: str) -> Optional[str]:
    words = _sub(r"\b(?:the|my|a|an)\b", " ", words)
    words = _WS.sub(" ", words).strip()
    if not words or not re.fullmatch(r"[a-z][a-z' ]*", words) or len(words.split()) > 4:
        return None
    if all(w in _LABEL_STOP for w in words.split()):
        return None
    return words


def _explicit_label(text: str) -> tuple[Optional[str], str]:
    """A "called X" label and the text without it."""
    m = _EXPLICIT_LABEL.search(text)
    if not m:
        return None, text
    label_text = m.group(1)
    # "called tea for 3 minutes": the label stops where the time or duration starts.
    label_text = re.split(r"\b(?:for|at|in)\b|\d", label_text)[0]
    rest = text[:m.start()] + " " + m.group(1)[len(label_text):]
    return _clean_label(label_text), _WS.sub(" ", rest).strip()


def _timer_label(duration: Duration) -> Optional[str]:
    # "set a timer for 10 minutes for the pasta"
    m = re.match(r"^(?:for|on) (.+)$", duration.after)
    if m:
        label = _clean_label(m.group(1))
        if label:
            return label
    # "set a pasta timer for 10 minutes", "a 10 minute egg timer"
    m = re.search(r"\b([a-z']+) timer\b", f"{duration.before} {duration.after}")
    if m and m.group(1) not in _LABEL_STOP:
        return _clean_label(m.group(1))
    return None


# --- gates -------------------------------------------------------------------

_SET_VERB = r"\b(?:set|make|create|add|put|start|schedule|need|want|give|new|begin)\b"

_TIMER_WORD = re.compile(r"\b(?:timer|count ?down)\b")
_TIMER_NOT = re.compile(
    r"\b(?:cancel|stop|delete|remove|clear|end|pause|resume|restart|reset|snooze|silence|"
    r"dismiss|turn off|disable|how (?:much|long)|left|remaining|status|check|list|show|"
    r"what|when|is there|are there|any)\b")

_ALARM_WORD = re.compile(r"\balarm\b|\bwake me\b|\bwake up call\b|\bget me up\b")
_ALARM_NOT = re.compile(
    r"\b(?:cancel|stop|delete|remove|clear|turn off|disable|snooze|silence|dismiss|change|"
    r"move|reset|edit|update|skip|is (?:ringing|going off|beeping)|went off|fire|smoke|car|"
    r"burglar)\b")
_WAKE = re.compile(r"\bwake me\b|\bwake up call\b|\bget me up\b")
# SET_ALARM takes no date: Android rings at the next matching time. An alarm for
# another day can't be set that way, so it isn't taken (it's left to the hub).
_OTHER_DAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekdays?|weekends?|"
    r"every|each|daily|next week|day after tomorrow|in \d+ days|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"\d+(?:st|nd|rd|th))\b")

_SHOW_ALARMS = re.compile(
    r"\b(?:show|open|see|view|list|display|check|pull up|bring up|go to|read)\b.*\balarms?\b|"
    r"\bwhat alarms\b|\bwhich alarms\b|\b(?:any|my) alarms\b|"
    r"\bwhat (?:time )?(?:is|are) my (?:next )?alarms?(?: set)?\b|\bwhen is my (?:next )?alarm\b|"
    r"\bdo i have (?:an |any )?alarms?\b|\bis my alarm set\b|\bwhat(?:'s| is) my (?:next )?alarm\b")


def _has_set_intent(text: str, noun: str) -> bool:
    """A set verb, or the noun leads ("timer for 10 minutes", "6:30 alarm", "a 10 minute timer")."""
    if re.search(_SET_VERB, text):
        return True
    return bool(re.match(rf"^(?:an? |the |my )?(?:{noun})\b", text)
                or re.match(rf"^(?:an? )?\S*\d.*\b(?:{noun})$", text))


# --- clock times -------------------------------------------------------------

_AM = re.compile(r"\bam\b|\bmorning\b")
_PM = re.compile(r"\bpm\b|\bafternoon\b|\bevening\b|\btonight\b|\bat night\b|\bin the night\b")


@dataclass(frozen=True)
class ClockTime:
    hour: int     # as said: 1-12, or 0-23 when ``exact``
    minute: int
    exact: bool   # a 24-hour reading ("18:30", "06:30", "midnight"); no a.m./p.m. guess needed


def _clock_from_patterns(text: str) -> Optional[ClockTime]:
    if re.search(r"\b(?:noon|midday)\b", text):
        return ClockTime(12, 0, True)
    if re.search(r"\bmidnight\b", text):
        return ClockTime(0, 0, True)

    def clock(h: int, m: int, exact: bool = False) -> Optional[ClockTime]:
        if 0 <= h <= 23 and 0 <= m <= 59:
            return ClockTime(h, m, exact or h == 0 or h > 12)
        return None

    m = re.search(rf"\b{_HALF} (?:past|after) (\d{{1,2}})\b", text)
    if m:
        return clock(int(m.group(1)), 30)
    m = re.search(rf"\b(?:a )?{_QUARTER} (?:past|after) (\d{{1,2}})\b", text)
    if m:
        return clock(int(m.group(1)), 15)
    m = re.search(rf"\b(?:a )?{_QUARTER} (?:to|till|til|before|of) (\d{{1,2}})\b", text)
    if m:
        return clock((int(m.group(1)) - 1) % 12 or 12, 45)
    m = re.search(r"\b(\d{1,2}) (?:minutes? )?(?:past|after) (\d{1,2})\b", text)
    if m:
        return clock(int(m.group(2)), int(m.group(1)))
    m = re.search(r"\b(\d{1,2}) (?:minutes? )?(?:to|till|til|before) (\d{1,2})\b", text)
    if m and 0 < int(m.group(1)) < 60:
        return clock((int(m.group(2)) - 1) % 12 or 12, 60 - int(m.group(1)))
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
    if m:
        return clock(int(m.group(1)), int(m.group(2)), exact=m.group(1).startswith("0"))
    m = re.search(r"\b(\d{1,2}) oh (\d)\b", text)
    if m:
        return clock(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d)(\d{2})\b(?= ?(?:am|pm|oclock|hours)\b)|\b(\d{2})(\d{2})\b", text)
    if m:
        h, mm = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return clock(int(h), int(mm), exact=h.startswith("0") or int(h) > 12)
    m = re.search(r"\b(\d{1,2}) (\d{2})\b", text)
    if m:
        return clock(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,2}) ?(?:am|pm|oclock)\b", text)
    if m:
        return clock(int(m.group(1)), 0)
    m = re.search(r"\b(?:at|for|to|by|around|about) (\d{1,2})\b(?! (?:minutes?|hours?|seconds?)\b)",
                  text)
    if m:
        return clock(int(m.group(1)), 0)
    return None


def _clock_from_parser(text: str, now: datetime) -> Optional[ClockTime]:
    """ovos-date-parser's reading, for phrasings the patterns miss."""
    if not re.search(r"\d", text):
        return None  # "tomorrow" alone would come back as a date, not a time
    try:
        found = extract_datetime(text, PARSE_LANG, anchorDate=now)
    except Exception as e:
        LOG.debug(f"extract_datetime failed on {text!r}: {e}")
        return None
    if not found:
        return None
    when = found[0]
    if (when.hour, when.minute) == (now.hour, now.minute):
        return None
    return ClockTime(when.hour, when.minute, True)


def _resolve(clock: ClockTime, text: str, now: datetime) -> tuple[int, int]:
    """The 24-hour wall-clock time, guessing a.m. or p.m. when it wasn't said."""
    hour, minute = clock.hour, clock.minute
    if clock.exact:
        return hour, minute
    if _PM.search(text):
        return (hour % 12) + 12, minute
    if _AM.search(text) or _WAKE.search(text) or re.search(r"\btomorrow\b", text):
        # "wake me up at 7" and "alarm for 7 tomorrow" are mornings. "12 am" is midnight,
        # but a bare "12" with a morning cue stays noon.
        return (0 if hour == 12 and re.search(r"\bam\b", text) else hour), minute
    # Neither said nor implied: the next time the clock shows it on the phone.
    for candidate in sorted({hour % 12, hour % 12 + 12}):
        if (candidate, minute) > (now.hour, now.minute):
            return candidate, minute
    return hour % 12, minute


_RELATIVE = re.compile(r"\b(?:in|after|for)\b ")


def _relative_time(text: str, now: datetime) -> Optional[tuple[int, int]]:
    """ "in 20 minutes", "for 8 hours from now": now plus the duration, on the phone's clock."""
    for m in _RELATIVE.finditer(text):
        duration = find_duration(text[m.end():])
        # "10 minutes past 6" is a clock time, not "in 10 minutes".
        if (duration and not duration.before
                and not re.match(r"(?:past|after|to|till|til|before)\b", duration.after)):
            when = now + timedelta(seconds=duration.seconds)
            return when.hour, when.minute
    return None


def alarm_time(text: str, now: datetime) -> Optional[tuple[int, int]]:
    """The hour and minute an alarm utterance asks for, in ``now``'s timezone."""
    relative = _relative_time(text, now)
    if relative:
        return relative
    clock = _clock_from_patterns(text) or _clock_from_parser(text, now)
    return _resolve(clock, text, now) if clock else None


# --- parse results -----------------------------------------------------------

MISSING_DURATION = "duration"  # timer.set without a length
MISSING_TIME = "time"          # alarm.set without a time
MISSING_BODY = "body"          # message.compose without the message


@dataclass(frozen=True)
class Parsed:
    """A request read from an utterance.

    ``missing`` names what the user still has to say (a follow-up question
    asks for it). ``check_app`` marks an ``app.open`` said without the word
    "app" ("open spotify"), which is taken only if the phone has such an app,
    so that "open the garage door" isn't.
    """
    request: str
    params: dict = field(default_factory=dict)
    missing: Optional[str] = None
    check_app: bool = False

    @property
    def complete(self) -> bool:
        return self.missing is None

    def request_or_none(self) -> Optional[WaggleRequest]:
        """The :class:`WaggleRequest`, if nothing is missing and nothing left to check."""
        if not self.complete or self.check_app:
            return None
        return WaggleRequest(self.request, self.params, speak=True)


def _bare_timer_label(text: str) -> Optional[str]:
    """ "start a pasta timer" -> "pasta"."""
    m = re.search(r"\b([a-z']+) timer\b", text)
    if m and m.group(1) not in _LABEL_STOP:
        return _clean_label(m.group(1))
    return None


# --- calendar ----------------------------------------------------------------

_CAL_NOUN = re.compile(r"\b(?:appointments?|meetings?|events?|calendar|schedule|agenda)\b")
_CAL_ASKS = re.compile(
    r"\b(?:what|what's|whats|when|when's|where|where's|which|do i have|have i got|is there|"
    r"are there|anything|any|next|read|tell me|show|check|list|give me)\b")
_CAL_NOT = re.compile(
    r"\b(?:add|create|make|book|cancel|delete|remove|move|reschedule|change|set up|invite|"
    r"accept|decline|put|new|share|sync|clear)\b|\bschedule (?:a|an|my|the|it)\b")
_CAL_FREE = re.compile(
    r"\b(?:what do i have|what have i got|what's happening|whats happening|what's going on|"
    r"am i (?:busy|free)|what are my plans|what's my day look like|how does my day look)\b")
# "my meeting", "any appointments", "next event": the user's own, not "the event horizon".
_CAL_MINE = re.compile(r"\b(?:my|any|next|upcoming|coming up)\b")
_NEXT_ONE = re.compile(r"\bnext (?:appointment|meeting|event)\b|\bwhen (?:is|'s) my\b|"
                       r"\bwhere (?:is|'s) my\b")
_NEXT_N = re.compile(r"\bnext (\d{1,2}) (?:appointments|meetings|events|things)\b")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def calendar_day(text: str, now: datetime) -> Optional[date]:
    """The day a calendar question is about, on the phone's calendar, or None for "next"."""
    today = now.date()
    if re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", text):
        return today
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    m = re.search(rf"\b(?:on |this |next )?({'|'.join(_WEEKDAYS)})\b", text)
    if m:
        ahead = (_WEEKDAYS.index(m.group(1)) - today.weekday()) % 7
        if ahead == 0 and re.search(rf"\bnext {m.group(1)}\b", text):
            ahead = 7
        return today + timedelta(days=ahead)
    return None


def _parse_calendar(text: str, now: datetime) -> Optional[Parsed]:
    noun = _CAL_NOUN.search(text)
    if _CAL_NOT.search(text):
        return None
    day = calendar_day(text, now)
    mine = _CAL_MINE.search(text)
    if not ((noun and mine and _CAL_ASKS.search(text)) or (_CAL_FREE.search(text) and (noun or day))):
        return None
    if day is not None:
        return Parsed(CALENDAR_NEXT, {"day": day.isoformat()})
    m = _NEXT_N.search(text)
    if m:
        return Parsed(CALENDAR_NEXT, {"count": max(1, min(10, int(m.group(1))))})
    if _NEXT_ONE.search(text):
        return Parsed(CALENDAR_NEXT, {"count": 1})
    return Parsed(CALENDAR_NEXT, {"count": 3})


# --- contacts: calls and texts -----------------------------------------------

_PHONE_TYPE_WORDS = {"cell": "cell", "cell phone": "cell", "mobile": "cell", "home": "home",
                     "work": "work", "office": "work"}
# For choosing among a contact's numbers by type.
PHONE_TYPE_SYNONYMS = {"cell phone": "cell", "mobile": "cell", "office": "work"}
_TYPE = r"(?P<type>cell phone|cell|mobile|home|work|office)"
_NOT_A_NAME = re.compile(
    r"^(?:me|it|him|her|them|us|this|that|back|off|out|up|in|an?|the police|for|someone|"
    r"anyone|somebody|everyone)\b")


def _clean_name(name: str) -> Optional[str]:
    """A contact name as said: "my mom" -> "mom"; None for "me a taxi", "it a day"."""
    name = _WS.sub(" ", name).strip()
    if not name or _NOT_A_NAME.match(name):
        return None
    name = _sub(r"^(?:my|the|our) ", "", name)
    if len(name.split()) > 4:
        return None
    return name or None


def _name_and_type(rest: str) -> tuple[Optional[str], Optional[str]]:
    """ "mom on her cell" -> ("mom", "cell"); "dad's work number" -> ("dad", "work")."""
    m = (re.fullmatch(rf"(?P<name>.+?) (?:on|at) (?:(?:his|her|their|the) )?{_TYPE}"
                      rf"(?: number| phone| line)?", rest)
         or re.fullmatch(rf"(?P<name>.+?)(?:'s)? {_TYPE}(?: number| phone| line)?", rest))
    if m:
        return _clean_name(m.group("name")), _PHONE_TYPE_WORDS[m.group("type")]
    return _clean_name(_sub(r"'s$", "", rest)), None


_CALL = re.compile(r"^(?:call|phone|dial|ring|place a call to|make a call to|"
                   r"start a call (?:to|with)|video call) (?P<rest>.+)$")
_GIVE_CALL = re.compile(r"^give (?P<rest>.+?) a (?:call|ring)$")


def _parse_call(text: str) -> Optional[Parsed]:
    m = _CALL.match(text) or _GIVE_CALL.match(text)
    if not m:
        return None
    rest = _sub(r"\s+(?:now|for me|right now|please)$", "", m.group("rest"))
    name, phone_type = _name_and_type(rest)
    if not name:
        return None
    params = {"name": name}
    if phone_type:
        params["type"] = phone_type
    return Parsed(CONTACT_CALL, params)


# Matched against the utterance as said (see _as_said), so the body keeps its case and
# punctuation.
_SAYING = r"(?:saying|that says|that|and say|to say|and tell (?:him|her|them)|with the message)"
_TEXT_TO = re.compile(
    rf"^(?:send|write|compose|draft)? ?(?:an? )?(?:text|sms|message|text message)(?: message)? "
    rf"(?:to|for) (?P<name>[^,:]+?)(?:(?:,? {_SAYING}|,|:) (?P<body>.+))?$", re.IGNORECASE)
_TEXT = re.compile(rf"^(?:text|message|sms) (?P<name>[^,:]+?)(?:(?:,? {_SAYING}|,|:) (?P<body>.+))?$",
                   re.IGNORECASE)


def _as_said(utterance: str) -> str:
    """The utterance with its case and punctuation, minus leading fillers and "on my phone"."""
    text = _WS.sub(" ", utterance.replace("’", "'")).strip()
    lead = _FILLERS.match(text.lower())
    text = text[lead.end():] if lead else text
    return _WS.sub(" ", re.sub(r"\b(?:on|to|in) (?:my|the) (?:phone|cell(?: phone)?|mobile)\b",
                               " ", text, flags=re.IGNORECASE)).strip()


def _parse_message(utterance: str) -> Optional[Parsed]:
    text = _as_said(utterance)
    m = _TEXT_TO.match(text) or _TEXT.match(text)
    if not m:
        return None
    name, body = m.group("name").lower(), m.group("body")
    if body is None and len(name.split()) > 2:
        # "text mom i'm running late": no word marks where the name ends, so the first
        # word is the name. The phone shows the message before it's sent.
        name, _ = name.split(" ", 1)
        body = m.group("name").split(" ", 1)[1]
    name = _clean_name(re.sub(r"[^\w' -]", " ", name))
    if not name:
        return None
    if body is None or not body.strip():
        return Parsed(MESSAGE_COMPOSE, {"name": name}, missing=MISSING_BODY)
    return Parsed(MESSAGE_COMPOSE, {"name": name, "body": body.strip()})


# --- apps --------------------------------------------------------------------

_OPEN = re.compile(r"^(?:open|launch|start|run|bring up|pull up|fire up|switch to)(?: up)? "
                   r"(?P<name>.+?)(?P<app> app(?:lication)?)?$")


def _parse_app(text: str) -> Optional[Parsed]:
    m = _OPEN.match(text)
    if not m:
        return None
    name = _sub(r"^(?:the|my|an?) ", "", m.group("name")).strip()
    if not name or len(name.split()) > 4:
        return None
    return Parsed(APP_OPEN, {"name": name}, check_app=m.group("app") is None)


# --- requests ----------------------------------------------------------------

def _parse_timer(text: str) -> Optional[Parsed]:
    if not _TIMER_WORD.search(text) or _TIMER_NOT.search(text) or not _has_set_intent(
            text, r"timer|count ?down"):
        return None
    label, rest = _explicit_label(text)
    duration = find_duration(rest)
    if duration is None:
        # "set a timer", "start a pasta timer": ask how long.
        label = label or _bare_timer_label(rest)
        return Parsed(TIMER_SET, {"label": label} if label else {}, missing=MISSING_DURATION)
    if not TIMER_MIN_S <= duration.seconds <= TIMER_MAX_S:
        return None
    label = label or _timer_label(duration)
    params = {"seconds": duration.seconds}
    if label:
        params["label"] = label
    return Parsed(TIMER_SET, params)


def _parse_alarm(text: str, now: datetime) -> Optional[Parsed]:
    if not _ALARM_WORD.search(text) or _ALARM_NOT.search(text) or _OTHER_DAY.search(text):
        return None
    if not _WAKE.search(text) and not _has_set_intent(text, "alarm"):
        return None
    label, rest = _explicit_label(text)
    when = alarm_time(rest, now)
    if when is None:
        if re.search(r"\d", rest):
            return None  # a number it can't read as a time: not a bare "set an alarm"
        return Parsed(ALARM_SET, {"label": label} if label else {}, missing=MISSING_TIME)
    params = {"hour": when[0], "minute": when[1]}
    if label:
        params["label"] = label
    return Parsed(ALARM_SET, params)


def parse_utterance(utterance: str, lang: str, now: datetime) -> Optional[Parsed]:
    """What an utterance asks for, complete or not, or None.

    ``now`` is the current time in the phone's timezone; relative and
    a.m./p.m.-less alarm times and calendar days are computed from it.
    """
    if not isinstance(utterance, str) or not str(lang or "").lower().startswith("en"):
        return None
    text = normalize(utterance)
    if not text:
        return None
    # "what time is my alarm set for" asks, it doesn't set.
    asks_only = not re.search(_SET_VERB, _sub(r"\balarms? (?:is |are )?set\b", "alarm", text))
    if _SHOW_ALARMS.search(text) and not _ALARM_NOT.search(text) and asks_only:
        return Parsed(ALARMS_SHOW, {})
    digits = digitize(text)
    if _TIMER_WORD.search(digits):
        return _parse_timer(digits)
    if _ALARM_WORD.search(digits):
        return _parse_alarm(digits, now)
    return (_parse_calendar(digits, now) or _parse_message(utterance) or _parse_call(text)
            or _parse_app(text))


def parse_request(utterance: str, lang: str, now: datetime) -> Optional[WaggleRequest]:
    """The complete request an utterance asks for, or None (see :func:`parse_utterance`)."""
    parsed = parse_utterance(utterance, lang, now)
    return parsed.request_or_none() if parsed else None


# --- follow-up answers -------------------------------------------------------

def answer_duration(answer: str) -> Optional[int]:
    """The timer length in an answer to "for how long?" ("10 minutes", "an hour"), or None."""
    duration = find_duration(digitize(normalize(answer)))
    if duration is None or not TIMER_MIN_S <= duration.seconds <= TIMER_MAX_S:
        return None
    return duration.seconds


def answer_alarm_time(asked: str, answer: str, now: datetime) -> Optional[tuple[int, int]]:
    """The alarm time in an answer to "for what time?".

    Read together with the request, so "wake me up" … "7" means 7 a.m.
    """
    answer = digitize(normalize(answer))
    if not re.search(r"\d|\bnoon\b|\bmidday\b|\bmidnight\b", answer):
        return None
    if re.match(r"\d", answer):
        answer = f"at {answer}"  # a bare "7" reads as a clock time, not a count
    return alarm_time(f"{digitize(normalize(asked))} {answer}", now)


_ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2,
             "3rd": 2, "three": 2, "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4,
             "five": 4}


def choose(answer: str, options: list[str],
           synonyms: Optional[dict[str, str]] = None) -> Optional[int]:
    """Which of ``options`` an answer picks ("Sam Lee", "the second one", "Lee"), or None.

    ``synonyms`` maps words of the answer to words of the options ("mobile" -> "cell").
    """
    text = normalize(answer)
    for word, replacement in (synonyms or {}).items():
        text = _sub(rf"\b{re.escape(word)}\b", replacement, text)
    if not text or not options:
        return None
    folded = [normalize(o) for o in options]
    if text in folded:
        return folded.index(text)
    m = re.search(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\b|"
                  r"^(?:the |number )?(one|two|three|four|five|\d)$", text)
    if m:
        word = m.group(1) or m.group(2)
        index = len(options) - 1 if word == "last" else (
            int(word) - 1 if word.isdigit() else _ORDINALS[word])
        return index if 0 <= index < len(options) else None
    # Words of the answer that pick out exactly one option: "Lee", "the work one".
    words = [w for w in text.split()
             if w not in ("the", "one", "number", "please", "my", "a", "his", "her", "their")]
    hits = [i for i, o in enumerate(folded) if words and all(re.search(rf"\b{re.escape(w)}\b", o)
                                                             for w in words)]
    return hits[0] if len(hits) == 1 else None
