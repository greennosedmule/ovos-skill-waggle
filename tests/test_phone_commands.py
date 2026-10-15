"""Phone settings, status and navigation (0.10.0): commands, phone.status, VIEW geo:, waggle_off."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from ovos_bus_client.message import Message

from ovos_skill_waggle.capabilities import CAN_ASK, PHONE_ACTIONS, WAGGLE_OFF, as_heard
from ovos_skill_waggle.parsing import parse_utterance, volume_level
from waggle import commands, intents, queries
from waggle.client import WaggleClient
from waggle.fake_phone import DEFAULT_CAPABILITIES, FakePhone
from waggle.messages import (
    COMMAND, COMMAND_RESPONSE, DND_SET, INTENT, INTENT_RESPONSE, QUERY, RINGER_SET, TORCH_SET,
    VOLUME_SET, Capabilities, Command, ErrorCode, Intent, Mode, WaggleError, is_command_action,
)
from waggle.queries import PhoneStatus
from waggle.rules import decide_command, limited

from conftest import PEER, capabilities, utterance
from test_handlers import request, run, spoken, watched
from test_phone_flags import flagged, match
from test_tools import ALL_TOOLS, offered, persona_query

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
COMMAND_RULES = [{"action": name, "mode": "run"}
                 for name in (TORCH_SET, VOLUME_SET, RINGER_SET, DND_SET)]
SETTINGS_TOOLS = {"set_flashlight", "set_volume", "set_ringer", "set_do_not_disturb"}


def older_phone():
    """A phone from before commands: no commands, no phone.status, no geo rule."""
    d = DEFAULT_CAPABILITIES.to_dict()
    d.pop("commands")
    d["queries"] = [q for q in d["queries"] if q != "phone.status"]
    d["rules"] = [r for r in d["rules"] if not r["action"].startswith("waggle.")
                  and r.get("scheme") != "geo"]
    return Capabilities.from_dict(d)


# --- the library: commands -----------------------------------------------------

@pytest.mark.parametrize("name,params", [
    (TORCH_SET, {"on": True}), (DND_SET, {"on": False}), (RINGER_SET, {"mode": "vibrate"}),
    (VOLUME_SET, {"stream": "alarm", "level": 0}), (VOLUME_SET, {"step": "up"}),
])
def test_command_round_trip(name, params):
    command = Command(id="c1", name=name, params=params, description="d")
    assert Command.from_dict(command.to_dict()) == command


@pytest.mark.parametrize("name,params", [
    ("waggle.phone.ring", {}), ("torch.set", {"on": True}),
    (TORCH_SET, {}), (TORCH_SET, {"on": "yes"}), (DND_SET, {"on": 1}),
    (RINGER_SET, {"mode": "loud"}), (VOLUME_SET, {}), (VOLUME_SET, {"level": 50, "step": "up"}),
    (VOLUME_SET, {"level": 101}), (VOLUME_SET, {"level": True}), (VOLUME_SET, {"step": "left"}),
    (VOLUME_SET, {"stream": "system", "level": 5}),
])
def test_bad_commands(name, params):
    with pytest.raises(WaggleError) as e:
        Command(id="c1", name=name, params=params)
    assert e.value.code is ErrorCode.BAD_REQUEST


def test_variants_and_stand_in_intents():
    assert commands.ringer("silent").as_intent().categories == ("silent",)
    assert commands.dnd(True).variant == "on" and commands.dnd(False).variant == "off"
    torch = commands.torch(True)
    stand_in = torch.as_intent()
    assert (stand_in.id, stand_in.action, stand_in.categories) == (torch.id, TORCH_SET, ())
    assert commands.volume(level=30).params == {"stream": "media", "level": 30}
    assert commands.volume("ring", step="down").description == "Turn the ring volume down"


def test_command_names_are_reserved_for_commands():
    assert is_command_action(TORCH_SET)
    assert not is_command_action("android.intent.action.VIEW")
    assert not is_command_action(None)


def test_capabilities_carry_commands():
    caps = DEFAULT_CAPABILITIES
    assert {TORCH_SET, VOLUME_SET, RINGER_SET, DND_SET} <= caps.commands
    assert Capabilities.from_dict(caps.to_dict()) == caps
    with pytest.raises(WaggleError):
        Capabilities.from_dict({**caps.to_dict(), "commands": "waggle.torch.set"})


def test_an_older_phone_reads_back_as_sent():
    old = older_phone()
    assert old.commands == frozenset()
    assert "commands" not in old.to_dict()


def test_decide_command_needs_the_announcement_and_a_rule():
    torch = commands.torch(True)
    assert decide_command(DEFAULT_CAPABILITIES, torch).mode is Mode.RUN
    assert decide_command(older_phone(), torch).mode is Mode.BLOCK
    # Announced, but no rule and unmatched block.
    caps = capabilities(rules=[], commands=[TORCH_SET])
    assert decide_command(caps, torch).mode is Mode.BLOCK
    caps = capabilities(rules=COMMAND_RULES
                        + [{"action": RINGER_SET, "category": "silent", "mode": "ask"}])
    assert decide_command(caps, commands.ringer("silent")).mode is Mode.ASK
    assert decide_command(caps, commands.ringer("vibrate")).mode is Mode.RUN


def test_limited_by_name():
    caps = limited(DEFAULT_CAPABILITIES, off=["phone.status", TORCH_SET, "unknown"])
    assert "phone.status" not in caps.queries and TORCH_SET not in caps.commands
    assert caps.rules == DEFAULT_CAPABILITIES.rules
    assert limited(DEFAULT_CAPABILITIES, off=["unknown"]) is DEFAULT_CAPABILITIES


# --- the library: phone.status and navigation ----------------------------------

def test_phone_status_full():
    status = PhoneStatus.from_dict({
        "battery": {"percent": 42, "charging": True, "source": "usb"},
        "network": {"type": "cellular", "metered": True}, "ringer": "vibrate", "dnd": True})
    assert (status.battery_percent, status.charging, status.source) == (42, True, "usb")
    assert (status.network, status.metered, status.ringer, status.dnd) == \
        ("cellular", True, "vibrate", True)
    assert PhoneStatus.from_dict(status.to_dict()) == status


def test_phone_status_parts_are_optional_and_values_may_be_new():
    assert PhoneStatus.from_dict({}) == PhoneStatus()
    status = PhoneStatus.from_dict({"battery": {"source": "dock"}, "network": {"type": "5g"},
                                    "ringer": "loud"})
    assert (status.source, status.network, status.ringer) == ("other", None, None)


@pytest.mark.parametrize("data", [
    {"battery": {"percent": 101}}, {"battery": {"percent": "50"}}, {"battery": "full"},
    {"battery": {"charging": "yes"}}, {"dnd": "on"}, {"ringer": 1}, [],
])
def test_bad_phone_status(data):
    with pytest.raises(WaggleError):
        PhoneStatus.from_dict(data)


def test_navigate_intent():
    intent = intents.navigate("123 Main St & 5th, Springfield")
    assert intent.action == "android.intent.action.VIEW" and intent.scheme == "geo"
    assert intent.data == "geo:0,0?q=123%20Main%20St%20%26%205th%2C%20Springfield"
    assert intent.description == "Directions to 123 Main St & 5th, Springfield"
    with pytest.raises(ValueError):
        intents.navigate("  ")


# --- the fake phone -------------------------------------------------------------

def test_fake_phone_runs_commands(bus, recorder):
    phone = FakePhone(bus)
    client = WaggleClient(bus)
    assert client.send_command(None, commands.torch(True), 1).ok and phone.torch
    response = client.send_command(None, commands.volume("ring", step="down"), 1)
    assert response.data == {"level": 40} and phone.volumes["ring"] == 40
    client.send_command(None, commands.ringer("silent"), 1)
    client.send_command(None, commands.dnd(True), 1)
    status = client.send_query(None, queries.phone_status(), 1)
    assert (status.data["ringer"], status.data["dnd"]) == ("silent", True)


def test_fake_phone_refuses_what_it_should(bus):
    client = WaggleClient(bus)
    FakePhone(bus, capabilities=older_phone())
    assert client.send_command(None, commands.torch(True), 1).error is ErrorCode.BLOCKED
    response = client.send_intent(None, Intent(id="i1", action=TORCH_SET), 1)
    assert response.error is ErrorCode.BAD_REQUEST


def test_fake_phone_without_access(bus):
    FakePhone(bus, permission_denied={DND_SET})
    response = WaggleClient(bus).send_command(None, commands.dnd(True), 1)
    assert response.error is ErrorCode.PERMISSION_DENIED


# --- parsing ---------------------------------------------------------------------

@pytest.mark.parametrize("said,request_name,params", [
    ("navigate to 123 Main Street", "navigate.to", {"destination": "123 main street"}),
    ("take me home", "navigate.to", {"destination": "home"}),
    ("directions to the nearest gas station", "navigate.to",
     {"destination": "nearest gas station"}),
    ("How do I get to the airport?", "navigate.to", {"destination": "airport"}),
    ("turn on the flashlight", "torch.set", {"on": True}),
    ("flashlight off", "torch.set", {"on": False}),
    ("turn the torch on", "torch.set", {"on": True}),
    ("turn on do not disturb", "dnd.set", {"on": True}),
    ("Do not disturb off.", "dnd.set", {"on": False}),
    ("disable DND", "dnd.set", {"on": False}),
    ("put my phone on silent", "ringer.set", {"mode": "silent"}),
    ("set my phone to vibrate", "ringer.set", {"mode": "vibrate"}),
    ("silence my phone", "ringer.set", {"mode": "silent"}),
    ("unmute my phone", "ringer.set", {"mode": "normal"}),
    ("turn off silent mode", "ringer.set", {"mode": "normal"}),
    ("vibrate mode", "ringer.set", {"mode": "vibrate"}),
    ("turn the ringer off", "ringer.set", {"mode": "silent"}),
    ("turn the volume up", "volume.set", {"step": "up", "stream": "media"}),
    ("volume down", "volume.set", {"step": "down", "stream": "media"}),
    ("increase the volume", "volume.set", {"step": "up", "stream": "media"}),
    ("set the volume to 50%", "volume.set", {"level": 50, "stream": "media"}),
    ("set the alarm volume to max", "volume.set", {"level": 100, "stream": "alarm"}),
    ("turn down the ringer volume", "volume.set", {"step": "down", "stream": "ring"}),
    ("volume to seven", "volume.set", {"level": 70, "stream": "media"}),
    ("mute the volume", "volume.set", {"level": 0, "stream": "media"}),
    ("what's my battery?", "phone.status", {"about": "battery"}),
    ("how much battery do I have left", "phone.status", {"about": "battery"}),
    ("is my phone charging", "phone.status", {"about": "battery"}),
    ("battery level", "phone.status", {"about": "battery"}),
    ("am I on wifi?", "phone.status", {"about": "network"}),
    ("do I have signal", "phone.status", {"about": "network"}),
    ("is my phone on silent", "phone.status", {"about": "sound"}),
    ("is do not disturb on?", "phone.status", {"about": "sound"}),
    ("phone status", "phone.status", {"about": "all"}),
    ("how's my phone", "phone.status", {"about": "all"}),
])
def test_parse(said, request_name, params):
    parsed = parse_utterance(said, "en-us", NOW)
    assert parsed is not None and (parsed.request, parsed.params) == (request_name, params)


@pytest.mark.parametrize("said", [
    "turn on do not disturb for an hour", "turn on battery saver", "turn on the lights",
    "turn up the heat", "lights off", "mute the tv", "set the volume to loud",
    "volume to 500",
])
def test_not_parsed(said):
    assert parse_utterance(said, "en-us", NOW) is None


@pytest.mark.parametrize("said", ["silence the timer", "turn off the alarm", "snooze"])
def test_stopping_still_wins(said):
    assert parse_utterance(said, "en-us", NOW).request in ("timers.stop", "alarm.dismiss",
                                                          "alarm.snooze")


@pytest.mark.parametrize("said,level", [
    ("max", 100), ("full", 100), ("zero", 0), ("0", 0), ("7", 70), ("10", 100),
    ("35 percent", 35), ("35%", 35), ("35", 35), ("150", None), ("loud", None),
])
def test_volume_level(said, level):
    assert volume_level(said) == level


# --- the handlers ----------------------------------------------------------------

@pytest.mark.parametrize("name,params,said", [
    ("torch.set", {"on": True}, "Flashlight on."),
    ("torch.set", {"on": False}, "Flashlight off."),
    ("dnd.set", {"on": True}, "Do Not Disturb is on."),
    ("ringer.set", {"mode": "silent"}, "Your phone is on silent."),
    ("ringer.set", {"mode": "normal"}, "Your ringer is on."),
    ("volume.set", {"level": 30}, "The media volume is now 30 percent."),
    ("volume.set", {"stream": "alarm", "step": "up"}, "The alarm volume is now 60 percent."),
])
def test_settings(bus, recorder, phone, name, params, said):
    response = run(bus, recorder, request(name, params))
    assert response.ok and spoken(recorder) == [said]
    sent = recorder.of(COMMAND)
    assert len(sent) == 1 and sent[0].context["destination"] == PEER


def test_volume_step_without_a_level_back(bus, recorder, pipeline, make_phone):
    phone = make_phone()
    phone._run_command = lambda command: None  # a phone that doesn't say the new level
    run(bus, recorder, request("volume.set", {"stream": "ring", "step": "down"}))
    assert spoken(recorder) == ["I've turned the ring volume down."]


def test_settings_watched_are_still_said(bus, recorder, phone):
    run(bus, recorder, watched(request("torch.set", {"on": True})))
    assert spoken(recorder) == ["Flashlight on."]


def test_a_variant_ask_rule(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=capabilities(
        rules=COMMAND_RULES + [{"action": RINGER_SET, "category": "silent", "mode": "ask"}],
        ask_timeout_s=0.3))
    response = run(bus, recorder, request("ringer.set", {"mode": "silent"}))
    assert response.ok
    assert spoken(recorder) == ["Confirm on your phone.", "Your phone is on silent."]
    run(bus, recorder, request("ringer.set", {"mode": "vibrate"}))
    assert spoken(recorder) == ["Your phone is on vibrate."]


def test_an_older_phone_gets_no_command(bus, recorder, pipeline, make_phone):
    make_phone(capabilities=older_phone())
    response = run(bus, recorder, request("torch.set", {"on": True}))
    assert response.error == "blocked" and recorder.of(COMMAND) == []
    response = run(bus, recorder, request("phone.status"))
    assert response.error == "blocked"
    assert spoken(recorder) == ["Your phone doesn't share its status with the hub."]


def test_no_access(bus, recorder, pipeline, make_phone):
    make_phone(permission_denied={DND_SET})
    response = run(bus, recorder, request("dnd.set", {"on": True}))
    assert response.error == "permission_denied"


@pytest.mark.parametrize("about,said", [
    ("battery", ["Your phone's battery is at 80 percent."]),
    ("network", ["Your phone is on Wi-Fi."]),
    ("sound", ["Your ringer is on. Do Not Disturb is off."]),
    ("all", ["Your phone's battery is at 80 percent. Your phone is on Wi-Fi. "
             "Your ringer is on. Do Not Disturb is off."]),
])
def test_status(bus, recorder, phone, about, said):
    response = run(bus, recorder, request("phone.status", {"about": about}))
    assert response.ok and spoken(recorder) == said


@pytest.mark.parametrize("battery,said", [
    ({"percent": 42, "charging": True, "source": "ac"},
     "Your phone's battery is at 42 percent and charging."),
    ({"percent": 100, "charging": False, "source": "wireless"},
     "Your phone's battery is at 100 percent and plugged in."),
    ({"percent": 100, "charging": False, "source": "dock"},
     "Your phone's battery is at 100 percent and plugged in."),
])
def test_status_battery(bus, recorder, pipeline, make_phone, battery, said):
    make_phone(status={"battery": battery})
    run(bus, recorder, request("phone.status", {"about": "battery"}))
    assert spoken(recorder) == [said]


def test_status_the_phone_left_out(bus, recorder, pipeline, make_phone):
    make_phone(status={"network": {"type": "none"}})
    run(bus, recorder, request("phone.status", {"about": "battery"}))
    assert spoken(recorder) == ["Your phone didn't say."]
    run(bus, recorder, request("phone.status"))
    assert spoken(recorder) == ["Your phone has no network connection."]


def test_bad_status(bus, recorder, pipeline, make_phone):
    make_phone(status={"battery": {"percent": 900}})
    assert run(bus, recorder, request("phone.status")).error == "bad_request"


def test_navigate(bus, recorder, phone):
    response = run(bus, recorder, request("navigate.to", {"destination": "the zoo"}))
    assert response.ok and spoken(recorder) == ["Opening the zoo in your maps app."]
    assert recorder.of(INTENT)[0].data["data"] == "geo:0,0?q=the%20zoo"


def test_navigate_watched_is_quiet(bus, recorder, phone):
    assert run(bus, recorder, watched(request("navigate.to", {"destination": "zoo"}))).ok
    assert spoken(recorder) == []


# --- waggle_off ------------------------------------------------------------------

def test_waggle_off_limits_the_phone(pipeline, make_phone):
    make_phone()
    phone = pipeline.capabilities.get(PEER)
    heard = as_heard(phone, flagged(utterance("x"), **{WAGGLE_OFF: ["phone.status",
                                                                    TORCH_SET]}))
    assert "phone.status" not in heard.capabilities.queries
    assert TORCH_SET not in heard.capabilities.commands
    for value in ([], ["something.else"], "phone.status", None, [1]):
        assert as_heard(phone, flagged(utterance("x"), **{WAGGLE_OFF: value})) is phone


def test_stage_steps_aside_for_waggle_off(pipeline, make_phone):
    make_phone()
    assert match(pipeline, "turn on the flashlight") is not None
    assert match(pipeline, "turn on the flashlight", **{WAGGLE_OFF: [TORCH_SET]}) is None
    assert match(pipeline, "what's my battery", **{WAGGLE_OFF: ["phone.status"]}) is None
    assert match(pipeline, "turn on the flashlight", **{PHONE_ACTIONS: False}) is None
    # A run rule needs no asking.
    assert match(pipeline, "turn on the flashlight", **{CAN_ASK: False}) is not None


def test_handler_says_why_for_waggle_off(bus, recorder, phone):
    response = run(bus, recorder, flagged(request("phone.status"),
                                          **{WAGGLE_OFF: ["phone.status"]}))
    assert response.error == "blocked"
    assert spoken(recorder) == ["Your phone doesn't allow that right now."]
    assert recorder.of(QUERY) == []


def test_stage_leaves_an_older_phone_alone(pipeline, make_phone):
    make_phone(capabilities=older_phone())
    for said in ("turn on the flashlight", "what's my battery", "navigate to the zoo",
                 "volume up"):
        assert match(pipeline, said) is None, said


# --- the persona's tools -------------------------------------------------------------

@pytest.fixture
def toolbox():
    from ovos_skill_waggle.tools import WaggleToolBox
    return WaggleToolBox()


def test_older_phone_gets_no_new_tools(toolbox, pipeline, make_phone):
    make_phone(capabilities=older_phone())
    # Notifications and location are still listed: older_phone drops only 0.10.0's additions.
    assert offered(toolbox, persona_query()) == ALL_TOOLS - SETTINGS_TOOLS - {
        "get_phone_status", "navigate_to", "reply_to_notification"}


def test_waggle_off_hides_tools(toolbox, pipeline, make_phone):
    make_phone()
    query = flagged(persona_query(), **{WAGGLE_OFF: ["phone.status", TORCH_SET]})
    assert offered(toolbox, query) == ALL_TOOLS - {"get_phone_status", "set_flashlight"}


def test_set_volume_tool_needs_one_of_level_and_step(toolbox, pipeline, make_phone):
    from test_tools import as_persona
    phone = make_phone()
    tool = toolbox.tools["set_volume"]
    result = as_persona(persona_query(), lambda: tool.tool_call(
        tool.argument_schema(level=20, step="up")))
    assert result.error == "bad_request"
    result = as_persona(persona_query(), lambda: tool.tool_call(
        tool.argument_schema(stream="ring", level=20)))
    assert result.ok and phone.volumes["ring"] == 20 and not result.on_screen


def without_torch():
    """The default rules, but no flashlight among the commands (a phone without a flash)."""
    return capabilities(commands=sorted(DEFAULT_CAPABILITIES.commands - {TORCH_SET}))


def test_a_rule_alone_doesnt_allow_an_unannounced_command(bus, recorder, pipeline, make_phone,
                                                          toolbox):
    caps = without_torch()
    assert decide_command(caps, commands.torch(True)).mode is Mode.BLOCK
    make_phone(capabilities=caps)
    assert match(pipeline, "turn on the flashlight") is None
    assert offered(toolbox, persona_query()) == ALL_TOOLS - {"set_flashlight"}
    response = run(bus, recorder, request("torch.set", {"on": True}))
    assert response.error == "blocked" and recorder.of(COMMAND) == []
