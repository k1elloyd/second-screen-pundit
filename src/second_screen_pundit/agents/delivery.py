"""Delivery tiers from prompts/commentator-persona.md: how each kind of line is voiced.

The tier is assigned by the server from the event or signal, never chosen by the model, so
the same moment is always voiced the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from second_screen_pundit.agents.models import DeliveryTier, Tone
from second_screen_pundit.engine.schema import Event

LATE_GO_AHEAD_MINUTE = 75


@dataclass(frozen=True)
class TierStyle:
    style: str
    # None leaves styledegree out: Learn documents no styledegree for MAI voices.
    style_degree: float | None = None


@dataclass(frozen=True)
class VoiceProfile:
    """The styles one voice supports on Learn, and the style each tier gets with it."""

    supported_styles: frozenset[str]
    tier_styles: Mapping[DeliveryTier, TierStyle]

    def style_for(self, tier: DeliveryTier | None) -> TierStyle | None:
        """The tier's style, or None when the tier has none or the voice doesn't support it."""
        style = self.tier_styles.get(tier) if tier is not None else None
        if style is None or style.style not in self.supported_styles:
            return None
        return style


# en-GB-Harry (MAI-Voice-2.1 and -Flash), styles from the Learn MAI-Voice voice table.
HARRY_PROFILE = VoiceProfile(
    supported_styles=frozenset({
        "agent", "angry", "audiobook", "customer_call_center", "disgusted", "educational",
        "fearful", "joyful", "narrator", "neutral", "sad", "surprised",
    }),
    tier_styles={
        # Late drama: a go-ahead goal from 75', a big chance, a red card.
        "goal_peak": TierStyle("surprised"),
        "big_chance": TierStyle("surprised"),
        "red_card": TierStyle("surprised"),
        "goal_equaliser": TierStyle("joyful"),
        "goal": TierStyle("joyful"),
        "formation": TierStyle("narrator"),
        "tactical": TierStyle("narrator"),
        "substitution": TierStyle("neutral"),
        "booking": TierStyle("neutral"),
        "routine": TierStyle("neutral"),
    },
)

# en-GB-RyanNeural: Learn lists chat, cheerful, sad and whispering; only cheerful and chat fit.
RYAN_PROFILE = VoiceProfile(
    supported_styles=frozenset({"chat", "cheerful", "sad", "whispering"}),
    tier_styles={
        "goal_peak": TierStyle("cheerful", 1.7),
        "goal_equaliser": TierStyle("cheerful", 1.5),
        "goal": TierStyle("cheerful", 1.2),
        "formation": TierStyle("chat", 1.0),
        "tactical": TierStyle("chat", 1.0),
    },
)

# A voice not listed here gets no style at all.
NO_STYLE_PROFILE = VoiceProfile(frozenset(), {})
VOICE_PROFILES: dict[str, VoiceProfile] = {
    "en-GB-Harry": HARRY_PROFILE,
    "en-GB-RyanNeural": RYAN_PROFILE,
}


def voice_profile(voice: str) -> VoiceProfile:
    """Look a voice up by its name without the model suffix (en-GB-Harry:MAI-Voice-2.1-Flash)."""
    return VOICE_PROFILES.get(voice.split(":", 1)[0], NO_STYLE_PROFILE)

# The persona's tone column, so the model no longer has to return it.
TIER_TONES: dict[DeliveryTier, Tone] = {
    "goal_peak": "urgent",
    "goal_equaliser": "urgent",
    "goal": "urgent",
    "big_chance": "urgent",
    "red_card": "urgent",
    "formation": "analytical",
    "tactical": "analytical",
    "substitution": "measured",
    "booking": "measured",
    "routine": "measured",
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
