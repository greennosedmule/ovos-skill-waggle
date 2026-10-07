"""The ``waggle`` pipeline stage: an ovos-core 2.x pipeline plugin (see SPEC.md "The pipeline stage").

:class:`WagglePipeline` is both a ``ConfidenceMatcherPipeline`` (OPM's
pipeline plugin template) and an ``OVOSAbstractApplication`` (a skill that
runs without the skill manager), as ovos-persona's ``PersonaService`` is.
ovos-core's ``IntentService`` loads it through ``OVOSPipelineFactory`` from
the ``opm.pipeline`` entry point, with ``intents.<plugin id>`` from
``mycroft.conf`` as its config, and the hub enables it by naming
``ovos-waggle-pipeline-plugin-high`` in ``intents.pipeline`` right after
``ovos-converse-pipeline-plugin``.

Only the high tier matches. The stage has one place in the pipeline, and
its matches don't come in degrees: it takes a request only when all of
SPEC's conditions hold, and ``match_medium`` and ``match_low`` never match.

How a match runs: ovos-core emits the match as a reply to the utterance,
with type :data:`UTTERANCE_REQUEST` and data ``{**utterance data, request,
params, speak: true, utterance, lang}``, which is a ``waggle:request``'s data
under its own type. Its handler runs the request like any other, and is
registered with ``is_intent=True`` so ovos-workshop emits
``ovos.utterance.handled`` once the outcome has been spoken. Plain
``waggle:request`` messages (the persona's tool calls, other skills) are
handled without it, because whoever made them owns the utterance.

A request said without everything it needs ("set a timer", "text Mom")
matches with ``missing`` set in its data. Its handler asks for the rest with
``get_response`` (the converse stage hands the answer back), reads the answer
here, since only the stage parses language, and then runs the request.

The stage must stay fast, since every utterance passes through it: a dict
lookup for the peer, then regexes and the date parser, and only for phones.
"Open X" without the word "app" also checks the phone's app names, which
:class:`~ovos_skill_waggle.apps.AppCatalog` fetched in the background.
"""
from __future__ import annotations

from datetime import datetime, timezone
from os.path import dirname
from typing import Callable, Dict, List, Optional, Union

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager
from ovos_config.config import Configuration
from ovos_plugin_manager.templates.pipeline import ConfidenceMatcherPipeline, IntentHandlerMatch
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_workshop.app import OVOSAbstractApplication

from waggle.messages import CAPABILITIES, WaggleError

from ovos_skill_waggle.actions import phone_allows
from ovos_skill_waggle.apps import AppCatalog
from ovos_skill_waggle.capabilities import CapabilityCache, Phone, peer_of
from ovos_skill_waggle.handlers import RequestHandlers, phone_origin
from ovos_skill_waggle.parsing import (
    MISSING_BODY, MISSING_DURATION, MISSING_TIME, Parsed, answer_alarm_time, answer_duration,
    original_wording, parse_utterance,
)
from ovos_skill_waggle.requests import MESSAGE_COMPOSE, REQUEST, WaggleRequest
from ovos_skill_waggle.settings import Settings

PLUGIN_ID = "ovos-waggle-pipeline-plugin"
SKILL_ID = "ovos-skill-waggle.greennosedmule"
# The match type ovos-core emits for a request the stage parsed from an utterance.
UTTERANCE_REQUEST = "waggle:utterance"

# The question for each missing piece, and stand-ins that let the rule check run without it.
ASK_DIALOGS = {MISSING_DURATION: "ask_duration", MISSING_TIME: "ask_time",
               MISSING_BODY: "ask_body"}
PLACEHOLDERS = {MISSING_DURATION: {"seconds": 60}, MISSING_TIME: {"hour": 7, "minute": 0},
                MISSING_BODY: {"body": "-"}}


class WagglePipeline(ConfidenceMatcherPipeline, OVOSAbstractApplication):
    """The Waggle stage, request handlers and capability cache, in one plugin."""

    # Fetch app lists in a thread; tests turn it off to keep the bus traffic in order.
    fetch_apps_in_background = True

    def __init__(self, bus: Optional[Union[MessageBusClient, FakeBus]] = None,
                 config: Optional[Dict] = None):
        bus = bus or FakeBus()
        if config is None:
            config = Configuration().get("intents", {}).get(PLUGIN_ID, {})
        OVOSAbstractApplication.__init__(self, bus=bus, skill_id=SKILL_ID,
                                         resources_dir=dirname(__file__))
        ConfidenceMatcherPipeline.__init__(self, bus=bus, config=config)
        self.waggle_settings = Settings.from_config(self.config)
        self.capabilities = CapabilityCache()
        # The current time in UTC; tests replace it.
        self.clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self.apps = AppCatalog(self.bus, lambda: self.waggle_settings.response_timeout_s)
        self.handlers = RequestHandlers(self.bus, self.capabilities,
                                        lambda: self.waggle_settings,
                                        self._render, self._speak, ask=self._ask,
                                        clock=lambda: self.clock())
        self.add_event(CAPABILITIES, self.handle_capabilities, speak_errors=False)
        self.add_event(REQUEST, self.handle_request, speak_errors=False)
        # is_intent: ovos-workshop emits ovos.utterance.handled when the handler returns.
        self.add_event(UTTERANCE_REQUEST, self.handle_utterance_request, is_intent=True,
                       speak_errors=False)

    # --- bus events ----------------------------------------------------------

    def handle_capabilities(self, message: Message) -> None:
        phone = self.capabilities.update(message)
        if phone is not None:
            self.apps.refresh(phone, phone_origin(message, phone.peer),
                              wait=not self.fetch_apps_in_background)

    def handle_request(self, message: Message) -> None:
        self.handlers.handle(message)

    def handle_utterance_request(self, message: Message) -> None:
        data = message.data if isinstance(message.data, dict) else {}
        missing = data.get("missing")
        if missing is not None:
            params = self.ask_for_missing(message, missing)
            if params is None:
                return  # the user didn't say; why has been spoken
            data = {k: v for k, v in data.items() if k != "missing"}
            message = Message(message.msg_type, {**data, "params": params}, message.context)
        self.handlers.handle(message)

    def ask_for_missing(self, message: Message, missing: str) -> Optional[dict]:
        """Ask for what the request lacks; its params with the answer, or None."""
        data = message.data
        peer = peer_of(message)
        origin = phone_origin(message, peer) if peer else message
        params = dict(data.get("params") or {})
        dialog = ASK_DIALOGS.get(missing)
        if dialog is None:
            LOG.warning(f"Waggle: can't ask for {missing!r}")
            return None
        phone = self.capabilities.get(peer)
        for attempt in range(2):
            answer = self._ask(origin, dialog, {})
            if answer is None:
                self._speak(origin, self._render("declined", {}), "declined", {})
                return None
            value = self.read_answer(missing, answer, data.get("utterance") or "", phone)
            if value is not None:
                return {**params, **value}
            if attempt == 0:
                self._speak(origin, self._render("not_understood", {}), "not_understood", {})
        self._speak(origin, self._render("declined", {}), "declined", {})
        return None

    def read_answer(self, missing: str, answer: str, asked: str,
                    phone: Optional[Phone]) -> Optional[dict]:
        """The params an answer to the follow-up question gives, or None."""
        if missing == MISSING_DURATION:
            seconds = answer_duration(answer)
            return {"seconds": seconds} if seconds else None
        if missing == MISSING_TIME:
            when = answer_alarm_time(asked, answer, self.phone_now(phone))
            return {"hour": when[0], "minute": when[1]} if when else None
        if missing == MISSING_BODY:
            body = answer.strip()
            return {"body": body} if body else None
        return None

    # --- matching ------------------------------------------------------------

    def match_high(self, utterances: List[str], lang: str,
                   message: Message) -> Optional[IntentHandlerMatch]:
        """A request from a Waggle phone that the phone would allow, else None."""
        phone = self.capabilities.get(peer_of(message)) if message is not None else None
        if phone is None:
            return None  # not a Waggle phone, or one speaking a version we don't
        for utterance in utterances:
            parsed = self.parse(utterance, lang, phone)
            if parsed is None:
                continue
            if parsed.request == MESSAGE_COMPOSE:
                # The message as said, not as ovos-core's normalizer rewrote it.
                said = self.parse(original_wording(utterance, utterances), lang, phone)
                if (said is not None and said.request == MESSAGE_COMPOSE
                        and said.params.get("name") == parsed.params.get("name")):
                    parsed, utterance = said, original_wording(utterance, utterances)
            if parsed.check_app and not self.apps.has_app(phone.client_id, parsed.params["name"]):
                continue  # "open the garage door": not an app on this phone
            if not self.would_allow(phone, parsed):
                continue
            match_data = {"request": parsed.request, "params": dict(parsed.params),
                          "speak": True}
            if parsed.missing:
                match_data["missing"] = parsed.missing
            LOG.info(f"Waggle: {utterance!r} from {phone.client_id} -> "
                     f"{parsed.request} {parsed.params}"
                     + (f", asking for {parsed.missing}" if parsed.missing else ""))
            return IntentHandlerMatch(match_type=UTTERANCE_REQUEST, match_data=match_data,
                                      skill_id=self.skill_id, utterance=utterance)
        return None

    def match_medium(self, utterances: List[str], lang: str,
                     message: Message) -> Optional[IntentHandlerMatch]:
        return None

    def match_low(self, utterances: List[str], lang: str,
                  message: Message) -> Optional[IntentHandlerMatch]:
        return None

    def phone_now(self, phone: Optional[Phone]) -> datetime:
        """The current time on the phone's clock (the hub's if it announced no timezone)."""
        now = self.clock()
        tz = phone.tz if phone is not None else None
        return now.astimezone(tz) if tz is not None else now.astimezone()

    def parse(self, utterance: str, lang: str, phone: Phone) -> Optional[Parsed]:
        """The request in ``utterance``, with times read on the phone's clock."""
        try:
            return parse_utterance(utterance, lang, self.phone_now(phone))
        except Exception as e:  # never break the pipeline on an odd utterance
            LOG.exception(f"Waggle: failed to parse {utterance!r}: {e}")
            return None

    def would_allow(self, phone: Phone, request: Union[WaggleRequest, Parsed]) -> bool:
        """Enabled here, shared by the phone, and not blocked by its rules (an "ask" rule
        allows). A request still missing something is judged with a stand-in for it."""
        params = dict(request.params)
        if isinstance(request, Parsed) and request.missing:
            params = {**PLACEHOLDERS.get(request.missing, {}), **params}
        try:
            complete = WaggleRequest(request.request, params)
        except WaggleError:
            return False
        return phone_allows(phone.capabilities, complete, self.waggle_settings)

    # --- speech --------------------------------------------------------------

    def _ask(self, origin: Message, dialog: str, data: dict) -> Optional[str]:
        """Speak a question to the phone and wait for the answer (None: none, or "cancel").

        ``get_response`` speaks by forwarding the message it's given, so it gets
        one addressed to the phone (a reply to ``origin``). It also marks the
        session as waiting for this skill's answer, and that session travels with
        the question to the phone, which sends it back with the answer.
        """
        to_phone = origin.reply(origin.msg_type, origin.data)
        session_id = SessionManager.get(to_phone).session_id
        heard: list = []

        def on_answer(message: Message) -> None:
            # get_response returns only the first utterance, which ovos-core's normalizer
            # rewrote; keep them all to recover the answer as said.
            if SessionManager.get(message).session_id == session_id:
                heard[:] = message.data.get("utterances") or []

        event = f"{self.skill_id}.converse.get_response"
        self.bus.on(event, on_answer)
        try:
            answer = self.get_response(dialog, data, num_retries=1, message=to_phone,
                                       wait=False)
        finally:
            self.bus.remove(event, on_answer)
        return None if answer is None else original_wording(answer, heard)

    def _render(self, dialog: str, data: dict) -> str:
        return self.dialog_renderer.render(dialog, data)

    def _speak(self, origin: Message, text: str, dialog: str, data: dict) -> None:
        """Emit ``speak`` as OVOSSkill.speak does, but as a reply to ``origin`` (the phone)."""
        message = origin.reply("speak", {
            "utterance": text,
            "expect_response": False,
            "meta": {"skill": self.skill_id, "dialog": dialog, "data": data},
            "lang": self.lang,
        })
        message.context["skill_id"] = self.skill_id
        self.bus.emit(message)
