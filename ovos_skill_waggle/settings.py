"""Settings for the Waggle pipeline stage and handlers (see SPEC.md "Fallbacks and configuration").

They come from the plugin's config: the hub's ``mycroft.conf`` under
``intents`` → ``ovos-waggle-pipeline-plugin``, which is what ovos-core's
``OVOSPipelineFactory`` hands a pipeline plugin. Per-request keys spell the
request with underscores for dots: ``enable_timer_set``, ``package_alarm_set``.
Bad values are logged and replaced by the default, so a typo never stops the
plugin from loading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ovos_utils.log import LOG

DEFAULT_RESPONSE_TIMEOUT_S = 5.0
DEFAULT_ASK_MARGIN_S = 5.0
# The wait for an "ask" rule when the phone doesn't announce ask_timeout_s.
# WAGGLE.md has no default; Wiggins uses 15 s.
DEFAULT_ASK_TIMEOUT_S = 15.0


def request_key(prefix: str, request: str) -> str:
    """``enable_timer_set`` for (``enable``, ``timer.set``)."""
    return f"{prefix}_{request.replace('.', '_')}"


def _seconds(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        LOG.warning(f"Waggle: ignoring bad {key} {value!r}, using {default}")
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    response_timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S
    ask_margin_s: float = DEFAULT_ASK_MARGIN_S
    default_phone_peer: Optional[str] = None  # S4
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "Settings":
        config = dict(config or {})
        peer = config.get("default_phone_peer")
        return cls(response_timeout_s=_seconds(config, "response_timeout_s",
                                               DEFAULT_RESPONSE_TIMEOUT_S),
                   ask_margin_s=_seconds(config, "ask_margin_s", DEFAULT_ASK_MARGIN_S),
                   default_phone_peer=peer if isinstance(peer, str) and peer else None,
                   raw=config)

    def enabled(self, request: str) -> bool:
        """``enable_<request>``, default true. A disabled request is never matched or run."""
        value = self.raw.get(request_key("enable", request), True)
        return value if isinstance(value, bool) else True

    def package(self, request: str) -> Optional[str]:
        """``package_<request>``: restrict the request's intent to one app, or None."""
        value = self.raw.get(request_key("package", request))
        return value.strip() if isinstance(value, str) and value.strip() else None

    def ask_wait_s(self, phone_ask_timeout_s: Optional[float]) -> float:
        """How long to wait on an "ask" rule: the phone's ask timeout plus the margin."""
        ask = DEFAULT_ASK_TIMEOUT_S if phone_ask_timeout_s is None else phone_ask_timeout_s
        return ask + self.ask_margin_s
