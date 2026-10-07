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

The stage must stay fast, since every utterance passes through it: a dict
lookup for the peer, then regexes and the date parser, and only for phones.
"""
from __future__ import annotations

from datetime import datetime, timezone
from os.path import dirname
from typing import Callable, Dict, List, Optional, Union

from ovos_bus_client.client import MessageBusClient
from ovos_bus_client.message import Message
from ovos_config.config import Configuration
from ovos_plugin_manager.templates.pipeline import ConfidenceMatcherPipeline, IntentHandlerMatch
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_workshop.app import OVOSAbstractApplication

from waggle.messages import CAPABILITIES
from waggle.rules import decide_for

from ovos_skill_waggle.actions import SUPPORTED_REQUESTS, build_intent
from ovos_skill_waggle.capabilities import CapabilityCache, Phone, peer_of
from ovos_skill_waggle.handlers import RequestHandlers
from ovos_skill_waggle.parsing import parse_request
from ovos_skill_waggle.requests import REQUEST, WaggleRequest
from ovos_skill_waggle.settings import Settings

PLUGIN_ID = "ovos-waggle-pipeline-plugin"
SKILL_ID = "ovos-skill-waggle.greennosedmule"
# The match type ovos-core emits for a request the stage parsed from an utterance.
UTTERANCE_REQUEST = "waggle:utterance"


class WagglePipeline(ConfidenceMatcherPipeline, OVOSAbstractApplication):
    """The Waggle stage, request handlers and capability cache, in one plugin."""

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
        self.handlers = RequestHandlers(self.bus, self.capabilities,
                                        lambda: self.waggle_settings,
                                        self._render, self._speak)
        self.add_event(CAPABILITIES, self.handle_capabilities, speak_errors=False)
        self.add_event(REQUEST, self.handle_request, speak_errors=False)
        # is_intent: ovos-workshop emits ovos.utterance.handled when the handler returns.
        self.add_event(UTTERANCE_REQUEST, self.handle_utterance_request, is_intent=True,
                       speak_errors=False)

    # --- bus events ----------------------------------------------------------

    def handle_capabilities(self, message: Message) -> None:
        self.capabilities.update(message)

    def handle_request(self, message: Message) -> None:
        self.handlers.handle(message)

    def handle_utterance_request(self, message: Message) -> None:
        self.handlers.handle(message)

    # --- matching ------------------------------------------------------------

    def match_high(self, utterances: List[str], lang: str,
                   message: Message) -> Optional[IntentHandlerMatch]:
        """A request from a Waggle phone that the phone would allow, else None."""
        phone = self.capabilities.get(peer_of(message)) if message is not None else None
        if phone is None:
            return None  # not a Waggle phone, or one speaking a version we don't
        for utterance in utterances:
            request = self.parse(utterance, lang, phone)
            if request is not None and self.would_allow(phone, request):
                LOG.info(f"Waggle: {utterance!r} from {phone.client_id} -> "
                         f"{request.request} {request.params}")
                return IntentHandlerMatch(match_type=UTTERANCE_REQUEST,
                                          match_data=request.to_dict(),
                                          skill_id=self.skill_id,
                                          utterance=utterance)
        return None

    def match_medium(self, utterances: List[str], lang: str,
                     message: Message) -> Optional[IntentHandlerMatch]:
        return None

    def match_low(self, utterances: List[str], lang: str,
                  message: Message) -> Optional[IntentHandlerMatch]:
        return None

    def parse(self, utterance: str, lang: str, phone: Phone) -> Optional[WaggleRequest]:
        """The request in ``utterance``, with times read on the phone's clock."""
        now = self.clock()
        tz = phone.tz
        now = now.astimezone(tz) if tz is not None else now.astimezone()  # else the hub's
        try:
            return parse_request(utterance, lang, now)
        except Exception as e:  # never break the pipeline on an odd utterance
            LOG.exception(f"Waggle: failed to parse {utterance!r}: {e}")
            return None

    def would_allow(self, phone: Phone, request: WaggleRequest) -> bool:
        """Enabled here, and not blocked by the phone's rules (an "ask" rule allows)."""
        settings = self.waggle_settings
        if not settings.enabled(request.request) or request.request not in SUPPORTED_REQUESTS:
            return False
        try:
            intent = build_intent(request, settings)
        except ValueError:
            return False
        return decide_for(phone.capabilities, intent).allowed

    # --- speech --------------------------------------------------------------

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
