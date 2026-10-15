"""The WAGGLE.md rule matcher, shared by the phone and the hub's pre-check."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional

from waggle.messages import Capabilities, Command, Intent, Mode, Rule


@dataclass(frozen=True)
class Decision:
    """What the phone will do with an intent; ``rule`` is None when ``unmatched`` applied."""
    mode: Mode
    rule: Optional[Rule] = None

    @property
    def allowed(self) -> bool:
        return self.mode is not Mode.BLOCK


def rule_matches(rule: Rule, intent: Intent) -> bool:
    """A rule matches when every field it sets equals the intent's value."""
    if rule.action != intent.action:
        return False
    if rule.scheme is not None and rule.scheme != intent.scheme:
        return False
    if rule.package is not None and rule.package != intent.package:
        return False
    if rule.category is not None and rule.category not in intent.categories:
        return False
    return True


def decide(rules: Iterable[Rule], intent: Intent, unmatched: Mode = Mode.BLOCK) -> Decision:
    """The most specific matching rule wins; ties go to the stricter mode."""
    best = max((r for r in rules if rule_matches(r, intent)),
               key=lambda r: (r.specificity, r.mode.strictness), default=None)
    if best is None:
        return Decision(Mode(unmatched))
    return Decision(best.mode, best)


def decide_for(capabilities: Capabilities, intent: Intent) -> Decision:
    return decide(capabilities.rules, intent, capabilities.unmatched)


def decide_command(capabilities: Capabilities, command: Command,
                   package: Optional[str] = None) -> Decision:
    """The phone's decision on a command: blocked unless it announced the command, and
    then its rules, judging the command's stand-in intent (for a reply to ``package``)."""
    if command.name not in capabilities.commands:
        return Decision(Mode.BLOCK)
    return decide_for(capabilities, command.as_intent(package))


def query_enabled(capabilities: Capabilities, name: str) -> bool:
    return name in capabilities.queries


def limited(capabilities: Capabilities, *, can_ask: bool = True,
            actions: bool = True, off: Iterable[str] = (),
            voice: Iterable[str] = ()) -> Capabilities:
    """``capabilities`` as they apply to one utterance (WAGGLE.md "Common rules").

    Without ``actions`` every rule blocks and no query is enabled; without
    ``can_ask`` an "ask" blocks, since the phone can't ask its user now, except
    for the rule actions in ``voice``, which it can confirm by voice. The
    queries and commands named in ``off`` aren't available for this utterance.
    """
    voice = frozenset(voice)
    off = frozenset(off)
    if off & (capabilities.queries | capabilities.commands):
        capabilities = replace(capabilities, queries=capabilities.queries - off,
                               commands=capabilities.commands - off)
    if not actions:
        return replace(capabilities, unmatched=Mode.BLOCK, queries=frozenset(),
                       rules=tuple(replace(r, mode=Mode.BLOCK) for r in capabilities.rules))
    if not can_ask:
        return replace(capabilities,
                       unmatched=Mode.BLOCK if capabilities.unmatched is Mode.ASK
                       else capabilities.unmatched,
                       rules=tuple(replace(r, mode=Mode.BLOCK)
                                   if r.mode is Mode.ASK and r.action not in voice else r
                                   for r in capabilities.rules))
    return capabilities
