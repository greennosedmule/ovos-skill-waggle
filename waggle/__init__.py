"""Waggle: the protocol library behind ovos-skill-waggle (see WAGGLE.md).

This package must not import ``ovos_skill_waggle``, so it can move to its own
distribution later.
"""
from waggle.messages import (
    PROTOCOL_VERSION, SUPPORTED_VERSIONS, Capabilities, ErrorCode, Extra, Intent, Mode,
    Query, Response, Rule, WaggleError, new_request_id,
)
from waggle.rules import Decision, decide, decide_for, query_enabled

__all__ = [
    "PROTOCOL_VERSION", "SUPPORTED_VERSIONS", "Capabilities", "ErrorCode", "Extra", "Intent",
    "Mode", "Query", "Response", "Rule", "WaggleError", "new_request_id",
    "Decision", "decide", "decide_for", "query_enabled",
]
