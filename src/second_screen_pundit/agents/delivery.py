"""Delivery tiers from prompts/commentator-persona.md: how each kind of line is voiced.

The tier is assigned by the server from the event or signal, never chosen by the model, so
the same moment is always voiced the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from second_screen_pundit.agents.models import DeliveryTier
from second_screen_pundit.engine.schema import Event

LATE_GO_AHEAD_MINUTE = 75


@dataclass(frozen=True)
class TierStyle:
    style: str
    style_degree: float


TIER_STYLES: dict[DeliveryTier, TierStyle] = {
    "goal_peak": TierStyle("sports_commentary_excited", 1.7),
    "goal_equaliser": TierStyle("sports_commentary_excited", 1.5),
    "goal": TierStyle("sports_commentary_excited", 1.2),
    "big_chance": TierStyle("sports_commentary_excited", 1.0),
    "red_card": TierStyle("sports_commentary", 1.4),
    "formation": TierStyle("sports_commentary", 1.0),
    "tactical": TierStyle("sports_commentary", 0.9),
    "substitution": TierStyle("sports_commentary", 0.8),
    "booking": TierStyle("sports_commentary", 0.8),
    "routine": TierStyle("sports_commentary", 0.6),
}


def tier_for_event(event: Event, *, goal_context: str | None, clock: int) -> DeliveryTier:
    if event.type == "goal":
        if goal_context == "equaliser":
            return "goal_equaliser"
        if goal_context == "go-ahead goal" and clock >= LATE_GO_AHEAD_MINUTE:
            return "goal_peak"
        return "goal"
    if event.type == "card":
        return "red_card" if event.detail.get("colour") == "red" else "booking"
    if event.type == "substitution":
        return "substitution"
    if event.type == "formation_change":
        return "formation"
    if event.type == "shot":
        return "big_chance"
    return "routine"


def tier_for_signals(kinds: Sequence[str]) -> DeliveryTier:
    if "red_card" in kinds:
        return "red_card"
    if "formation_change" in kinds:
        return "formation"
    return "tactical"
