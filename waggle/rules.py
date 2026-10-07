"""The WAGGLE.md rule matcher, shared by the phone and the hub's pre-check."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from waggle.messages import Capabilities, Intent, Mode, Rule


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


def query_enabled(capabilities: Capabilities, name: str) -> bool:
    return name in capabilities.queries
