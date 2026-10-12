from __future__ import annotations

import pytest

from ovos_skill_waggle.settings import Settings, request_key


def test_defaults():
    s = Settings.from_config(None)
    assert (s.response_timeout_s, s.ask_margin_s) == (5.0, 5.0)
    assert s.enabled("timer.set") and s.package("timer.set") is None
    assert s.ask_wait_s(15) == 20.0
    assert s.ask_wait_s(None) == 20.0  # Wiggins' 15 s when the phone doesn't say


def test_values():
    s = Settings.from_config({"response_timeout_s": 2, "ask_margin_s": 1.5,
                              "enable_alarm_set": False, "package_timer_set": " com.example.clock "})
    assert (s.response_timeout_s, s.ask_margin_s) == (2.0, 1.5)
    assert not s.enabled("alarm.set") and s.enabled("timer.set")
    assert s.package("timer.set") == "com.example.clock"
    assert s.ask_wait_s(30) == 31.5


@pytest.mark.parametrize("value", [0, -1, "5", True, None, []])
def test_bad_timeouts_fall_back(value):
    s = Settings.from_config({"response_timeout_s": value, "ask_margin_s": value})
    assert (s.response_timeout_s, s.ask_margin_s) == (5.0, 5.0)


def test_bad_flags_and_packages_ignored():
    s = Settings.from_config({"enable_timer_set": "no", "package_timer_set": "  "})
    assert s.enabled("timer.set") and s.package("timer.set") is None


def test_request_key():
    assert request_key("enable", "timer.set") == "enable_timer_set"
    assert request_key("package", "message.compose") == "package_message_compose"


def test_capabilities_file(data_home, tmp_path):
    default = data_home / "ovos-skill-waggle" / "capabilities.json"
    assert Settings.from_config(None).capabilities_file() == default
    assert Settings.from_config({"capabilities_file": True}).capabilities_file() == default
    assert Settings.from_config({"capabilities_file": 5}).capabilities_file() == default
    assert Settings.from_config({"capabilities_file": False}).capabilities_file() is None
    assert Settings.from_config({"capabilities_file": ""}).capabilities_file() is None
    path = str(tmp_path / "phones.json")
    assert str(Settings.from_config({"capabilities_file": path}).capabilities_file()) == path
