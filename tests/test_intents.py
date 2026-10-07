from __future__ import annotations

import uuid

import pytest

from waggle import intents
from waggle.messages import Intent, Mode, Rule
from waggle.rules import decide

ID = "req-1"


def wire(intent: Intent) -> dict:
    d = intent.to_dict()
    assert Intent.from_dict(d) == intent  # every builder's output round-trips
    return d


def test_constants_are_the_android_names():
    assert intents.ACTION_SET_ALARM == "android.intent.action.SET_ALARM"
    assert intents.ACTION_SET_TIMER == "android.intent.action.SET_TIMER"
    assert intents.ACTION_SHOW_ALARMS == "android.intent.action.SHOW_ALARMS"
    assert intents.ACTION_MAIN == "android.intent.action.MAIN"
    assert intents.CATEGORY_LAUNCHER == "android.intent.category.LAUNCHER"
    assert intents.ACTION_DIAL == "android.intent.action.DIAL"
    assert intents.ACTION_SENDTO == "android.intent.action.SENDTO"
    assert intents.EXTRA_HOUR == "android.intent.extra.alarm.HOUR"
    assert intents.EXTRA_MINUTES == "android.intent.extra.alarm.MINUTES"
    assert intents.EXTRA_MESSAGE == "android.intent.extra.alarm.MESSAGE"
    assert intents.EXTRA_SKIP_UI == "android.intent.extra.alarm.SKIP_UI"
    assert intents.EXTRA_LENGTH == "android.intent.extra.alarm.LENGTH"
    assert intents.EXTRA_SMS_BODY == "sms_body"


# --- ids and package ---------------------------------------------------------

def test_new_request_id_by_default():
    a, b = intents.show_alarms(), intents.show_alarms()
    assert a.id != b.id
    uuid.UUID(a.id)


@pytest.mark.parametrize("build", [
    lambda **kw: intents.set_alarm(6, 30, **kw),
    lambda **kw: intents.set_timer(600, **kw),
    lambda **kw: intents.show_alarms(**kw),
    lambda **kw: intents.dial("123", **kw),
    lambda **kw: intents.compose_sms("123", "hi", **kw),
])
def test_package_override(build):
    assert build(id=ID).package is None
    assert build(id=ID, package="com.example.clock").package == "com.example.clock"
    with pytest.raises(ValueError):
        build(id=ID, package="")


def test_empty_id_rejected():
    with pytest.raises(ValueError):
        intents.show_alarms(id="")


# --- set_alarm ---------------------------------------------------------------

def test_set_alarm_wire():
    assert wire(intents.set_alarm(6, 30, id=ID)) == {
        "id": ID,
        "description": "Set an alarm for 6:30 AM",
        "action": "android.intent.action.SET_ALARM",
        "extras": {
            "android.intent.extra.alarm.HOUR": {"type": "int", "value": 6},
            "android.intent.extra.alarm.MINUTES": {"type": "int", "value": 30},
            "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": True},
        },
    }


def test_set_alarm_label_and_ui():
    d = wire(intents.set_alarm(18, 5, label="Gym", skip_ui=False, id=ID))
    assert d["description"] == 'Set an alarm for 6:05 PM labeled "Gym"'
    assert d["extras"]["android.intent.extra.alarm.MESSAGE"] == {"type": "string", "value": "Gym"}
    assert d["extras"]["android.intent.extra.alarm.SKIP_UI"] == {"type": "bool", "value": False}


def test_set_alarm_empty_label_is_no_label():
    assert "android.intent.extra.alarm.MESSAGE" not in intents.set_alarm(6, 0, label="").extras


@pytest.mark.parametrize("hour,minute,text", [
    (0, 0, "12:00 AM"), (0, 59, "12:59 AM"), (11, 59, "11:59 AM"),
    (12, 0, "12:00 PM"), (13, 1, "1:01 PM"), (23, 59, "11:59 PM"),
])
def test_set_alarm_clock_text(hour, minute, text):
    assert intents.set_alarm(hour, minute).description == f"Set an alarm for {text}"


@pytest.mark.parametrize("hour,minute", [
    (-1, 0), (24, 0), (0, -1), (0, 60), (6.0, 30), ("6", 30), (True, 30), (6, None),
])
def test_set_alarm_bad_time(hour, minute):
    with pytest.raises(ValueError):
        intents.set_alarm(hour, minute)


@pytest.mark.parametrize("kw", [{"label": 5}, {"skip_ui": 1}, {"skip_ui": None}])
def test_set_alarm_bad_options(kw):
    with pytest.raises(ValueError):
        intents.set_alarm(6, 30, **kw)


# --- set_timer ---------------------------------------------------------------

def test_set_timer_wire():
    assert wire(intents.set_timer(600, id=ID)) == {
        "id": ID,
        "description": "Start a 10-minute timer",
        "action": "android.intent.action.SET_TIMER",
        "extras": {
            "android.intent.extra.alarm.LENGTH": {"type": "int", "value": 600},
            "android.intent.extra.alarm.SKIP_UI": {"type": "bool", "value": True},
        },
    }


def test_set_timer_label():
    d = wire(intents.set_timer(300, label="Eggs", skip_ui=False, id=ID))
    assert d["description"] == 'Start a 5-minute timer labeled "Eggs"'
    assert d["extras"]["android.intent.extra.alarm.MESSAGE"] == {"type": "string", "value": "Eggs"}
    assert d["extras"]["android.intent.extra.alarm.SKIP_UI"]["value"] is False


@pytest.mark.parametrize("seconds,text", [
    (1, "a 1-second timer"),
    (8, "an 8-second timer"),
    (11, "an 11-second timer"),
    (45, "a 45-second timer"),
    (60, "a 1-minute timer"),
    (90, "a timer for 1 minute and 30 seconds"),
    (600, "a 10-minute timer"),
    (1080, "an 18-minute timer"),
    (3600, "a 1-hour timer"),
    (3661, "a timer for 1 hour, 1 minute and 1 second"),
    (5400, "a timer for 1 hour and 30 minutes"),
    (7205, "a timer for 2 hours and 5 seconds"),
    (28800, "an 8-hour timer"),
    (86400, "a 24-hour timer"),
])
def test_set_timer_description(seconds, text):
    assert intents.set_timer(seconds).description == f"Start {text}"


@pytest.mark.parametrize("seconds", [0, -1, 86401, 1.5, "60", True, None])
def test_set_timer_bad_length(seconds):
    with pytest.raises(ValueError):
        intents.set_timer(seconds)


# --- show_alarms -------------------------------------------------------------

def test_show_alarms_wire():
    assert wire(intents.show_alarms(id=ID)) == {
        "id": ID, "description": "Show alarms", "action": "android.intent.action.SHOW_ALARMS",
    }


# --- launch_app --------------------------------------------------------------

def test_launch_app_wire():
    assert wire(intents.launch_app("app.grapheneos.camera", label="Camera", id=ID)) == {
        "id": ID,
        "description": "Open Camera",
        "action": "android.intent.action.MAIN",
        "categories": ["android.intent.category.LAUNCHER"],
        "package": "app.grapheneos.camera",
    }


def test_launch_app_without_label_names_package():
    assert intents.launch_app("org.example.app").description == "Open org.example.app"


@pytest.mark.parametrize("package", ["", "  ", None, 3])
def test_launch_app_needs_package(package):
    with pytest.raises(ValueError):
        intents.launch_app(package)


def test_launch_app_matches_launcher_rule():
    rule = Rule("android.intent.action.MAIN", Mode.RUN, category="android.intent.category.LAUNCHER")
    assert decide([rule], intents.launch_app("org.example.app")).rule == rule


# --- dial and compose_sms ----------------------------------------------------

def test_dial_wire():
    assert wire(intents.dial("+15551234567", name="Mom", id=ID)) == {
        "id": ID,
        "description": "Call Mom",
        "action": "android.intent.action.DIAL",
        "data": "tel:+15551234567",
    }


def test_dial_without_name_names_number():
    assert intents.dial(" +15551234567 ").description == "Call +15551234567"


@pytest.mark.parametrize("number,uri", [
    ("+15551234567", "tel:+15551234567"),
    ("5551234", "tel:5551234"),
    ("+1 (555) 123-4567", "tel:+1%20(555)%20123-4567"),
    ("555.123.4567", "tel:555.123.4567"),
    ("*#06#", "tel:*%2306%23"),
    ("5551234,,22#", "tel:5551234%2C%2C22%23"),
    ("5551234;ext=9", "tel:5551234%3Bext%3D9"),
    ("100%", "tel:100%25"),
])
def test_number_uri_encoding(number, uri):
    assert intents.dial(number).data == uri
    assert intents.compose_sms(number, "x").data == "smsto:" + uri[len("tel:"):]


def test_dial_matches_tel_rule():
    rule = Rule("android.intent.action.DIAL", Mode.ASK, scheme="tel")
    assert decide([rule], intents.dial("*#06#")).mode is Mode.ASK


def test_compose_sms_wire():
    assert wire(intents.compose_sms("+15551234567", "Running late", name="Mom", id=ID)) == {
        "id": ID,
        "description": "Text Mom",
        "action": "android.intent.action.SENDTO",
        "data": "smsto:+15551234567",
        "extras": {"sms_body": {"type": "string", "value": "Running late"}},
    }


def test_compose_sms_without_name_names_number():
    assert intents.compose_sms("5551234", "hi").description == "Text 5551234"


@pytest.mark.parametrize("args", [
    ("", "hi"), ("   ", "hi"), (None, "hi"), (5551234, "hi"),
    ("5551234", ""), ("5551234", None), ("5551234", 3),
])
def test_compose_sms_bad_args(args):
    with pytest.raises(ValueError):
        intents.compose_sms(*args)


@pytest.mark.parametrize("number", ["", " ", None, 5551234])
def test_dial_bad_number(number):
    with pytest.raises(ValueError):
        intents.dial(number)


@pytest.mark.parametrize("name", ["", 7])
def test_bad_name(name):
    with pytest.raises(ValueError):
        intents.dial("5551234", name=name)
    with pytest.raises(ValueError):
        intents.compose_sms("5551234", "hi", name=name)

