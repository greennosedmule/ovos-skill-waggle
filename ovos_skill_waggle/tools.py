"""The persona's tools: Waggle requests as an ``opm.agents.toolbox`` plugin (see SPEC.md "The persona (S4)").

Requests the stage can't parse ("text Jenny I'm running late, then set a
timer for the oven") fall through to the hub's LLM persona. With
ovos-agentic-loop's ``ovos-native-toolcall-loop`` as the persona's solver and
this toolbox in its ``toolboxes``, the model can hand them back as requests:
one tool per request in SPEC's table. A tool call runs the request exactly as
a ``waggle:request`` with ``speak: false`` would, and returns its
``waggle:request.response`` (``ok``, ``summary``, ``error``) for the model to
put in its own words.

**Which phone asked.** The loop calls a tool with its arguments only, not the
message or the session. Everything runs on the thread of ovos-persona's
handler, which has the triggering message as an argument, so
``dig_for_message`` finds it on the stack: its peer is the phone, and
replies to it reach that phone. ovos-persona also loads this toolbox without
a bus, so the tools reach the request handlers through the Waggle plugin
running in the same ovos-core process (:func:`~ovos_skill_waggle.pipeline.find_phone`),
so the toolbox only works in that process, where ovos-persona runs too.

**What the model is offered.** The loop asks each toolbox for its tools every
time it calls the model, on the same thread, so :attr:`WaggleToolBox.tool_json_list`
lists only the requests the asking phone would allow, by the same check the
stage uses. A satellite that isn't a Waggle phone (or a persona query with no
message at all) gets none. The handlers check the phone's rules again when a
tool runs, so the list only keeps the model from trying what the phone
refuses.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from ovos_bus_client.message import Message, dig_for_message
from ovos_plugin_manager.templates.agent_tools import AgentTool, ToolArguments, ToolBox, ToolOutput
from ovos_utils.log import LOG
from pydantic import Field

from ovos_skill_waggle.actions import phone_allows
from ovos_skill_waggle.capabilities import Phone, peer_of
from ovos_skill_waggle.pipeline import WagglePipeline, find_phone
from ovos_skill_waggle.requests import (
    ALARM_SET, ALARMS_SHOW, APP_OPEN, CALENDAR_NEXT, CONTACT_CALL, MESSAGE_COMPOSE, REQUEST,
    TIMER_SET, UNREACHABLE, RequestResponse, WaggleRequest,
)

TOOLBOX_ID = "ovos-waggle-tools"
# How far up the stack to look for the persona's message. The loop, the model's
# engine and ovos-persona's generators sit between it and the tool.
DIG_DEPTH = 64
NOT_A_PHONE = "This request didn't come from a phone that allows phone actions."


# --- arguments -----------------------------------------------------------------

class SetAlarmArgs(ToolArguments):
    hour: int = Field(..., ge=0, le=23, description="Hour, 0-23, on the phone's clock.")
    minute: int = Field(..., ge=0, le=59, description="Minute, 0-59.")
    label: Optional[str] = Field(None, description="Optional name for the alarm.")


class SetTimerArgs(ToolArguments):
    seconds: int = Field(..., ge=1, le=86400, description="Length in seconds, at most 24 hours.")
    label: Optional[str] = Field(None, description="Optional name for the timer, e.g. 'oven'.")


class NoArgs(ToolArguments):
    pass


class CalendarArgs(ToolArguments):
    count: Optional[int] = Field(None, ge=1, le=10,
                                 description="How many upcoming events, 1-10 (default 1).")
    day: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                               description="A day to list instead, YYYY-MM-DD, on the "
                                           "phone's calendar.")


class AppArgs(ToolArguments):
    name: str = Field(..., min_length=1, description="The app's name as the user said it.")


class CallArgs(ToolArguments):
    name: str = Field(..., min_length=1,
                      description="A contact's name, or a phone number in digits.")
    type: Optional[Literal["cell", "home", "work", "other"]] = Field(
        None, description="Which of the contact's numbers, if the user said.")


class MessageArgs(ToolArguments):
    name: str = Field(..., min_length=1,
                      description="A contact's name, or a phone number in digits.")
    body: str = Field(..., min_length=1, description="The message text, as the user wants it "
                                                     "sent.")


class Outcome(ToolOutput):
    ok: bool = Field(..., description="Whether the phone did it.")
    summary: str = Field(..., description="What happened, in words.")
    error: Optional[str] = Field(None, description="Why not, when ok is false: blocked, "
                                                   "declined, timeout, no_handler, unreachable, "
                                                   "...")


# Per request: tool name, description, arguments, and params the rule check can judge it by
# before the model has chosen any.
TOOLS: Dict[str, Tuple[str, str, type, dict]] = {
    ALARM_SET: ("set_alarm",
                "Set an alarm on the user's phone, for the next time its clock shows "
                "hour:minute. Only today or tomorrow; there is no date.",
                SetAlarmArgs, {"hour": 7, "minute": 0}),
    TIMER_SET: ("set_timer", "Start a countdown timer on the user's phone.",
                SetTimerArgs, {"seconds": 60}),
    ALARMS_SHOW: ("show_alarms", "Open the alarm list on the user's phone screen.",
                  NoArgs, {}),
    CALENDAR_NEXT: ("get_calendar_events",
                    "Read the user's upcoming calendar events from their phone: the next "
                    "few, or those on one day.",
                    CalendarArgs, {}),
    APP_OPEN: ("open_app", "Open an app on the user's phone.", AppArgs, {"name": "camera"}),
    CONTACT_CALL: ("call_contact",
                   "Open the phone's dialer with a contact's number (or a number) filled "
                   "in. The user taps call; nothing is dialed until then.",
                   CallArgs, {"name": "5550100"}),
    MESSAGE_COMPOSE: ("compose_text_message",
                      "Open a text message to a contact (or a number) on the phone, with "
                      "the text filled in. The user taps send; nothing is sent until then.",
                      MessageArgs, {"name": "5550100", "body": "-"}),
}


# --- the toolbox ---------------------------------------------------------------

def _asking_message() -> Optional[Message]:
    return dig_for_message(max_records=DIG_DEPTH)


def _asking_phone() -> Tuple[Optional[Message], Optional[WagglePipeline], Optional[Phone]]:
    """The persona's message, the Waggle plugin, and the phone that asked (each may be None)."""
    message = _asking_message()
    pipeline, phone = find_phone(peer_of(message) if message is not None else None)
    return message, pipeline, phone


class WaggleToolBox(ToolBox):
    """Waggle's requests as tools for ovos-persona's tool-calling loop."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, bus: Optional[Any] = None):
        super().__init__(toolbox_id=TOOLBOX_ID, config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [AgentTool(name=name, description=description, argument_schema=args,
                          output_schema=Outcome, tool_call=self._caller(request))
                for request, (name, description, args, _) in TOOLS.items()]

    @property
    def tool_json_list(self) -> List[Dict[str, Any]]:
        """The tools the asking phone would allow, by its announced rules and queries."""
        message, pipeline, phone = _asking_phone()
        if phone is None:
            return []
        allowed = {TOOLS[request][0] for request, (_, _, _, params) in TOOLS.items()
                   if phone_allows(phone.capabilities, WaggleRequest(request, params),
                                   pipeline.waggle_settings)}
        return [tool for tool in super().tool_json_list if tool["name"] in allowed]

    def _caller(self, request: str) -> Callable[[ToolArguments], Outcome]:
        def call(args: ToolArguments) -> Outcome:
            return self.run(request, args.model_dump(exclude_none=True))
        return call

    def run(self, request: str, params: dict) -> Outcome:
        """Run ``request`` for the phone that asked, as a ``waggle:request`` with ``speak: false``."""
        message, pipeline, phone = _asking_phone()
        if phone is None:
            LOG.warning(f"Waggle: {request} tool called, but not for a Waggle phone")
            return Outcome(ok=False, summary=NOT_A_PHONE, error=UNREACHABLE)
        # The handler validates the params, and answers bad_request for any it rejects.
        data = {"request": request, "params": params, "speak": False}
        LOG.info(f"Waggle: persona tool {request} {params} for {phone.client_id}")
        response: RequestResponse = pipeline.handlers.handle(
            Message(REQUEST, data, dict(message.context or {})))
        return Outcome(**response.to_dict())
