"""Deterministic fallback lines, from the variants in prompts/commentator-persona.md.

A variant is picked at random, seeded by the line ID, so a replay always produces the same
line for the same moment. Each variant uses only placeholders filled from verified facts;
the caller still runs the evidence check and moves to the next variant if one fails.
"""

from __future__ import annotations

import random
import string
from collections.abc import Collection, Iterator, Mapping

FallbackKind = str

FALLBACK_VARIANTS: dict[FallbackKind, tuple[str, ...]] = {
    "goal": (
        "Goal for {team}. {score_phrase}.",
        "{player_name} scores for {team}. {score_phrase}.",
        "{player_name} takes {chance_descriptor} and {team} have their goal. {score_phrase}.",
        "{team} find the net through {player_name}. {score_phrase}.",
    ),
    "red_card": (
        "A red card for {team}. {score_phrase}.",
        "{player_name} is sent off, and {team} must reorganise a player short. {score_phrase}.",
        "Off goes {player_name}. {team} will have to manage a player light. {score_phrase}.",
    ),
    "booking": (
        "A {colour} card for {team}. {score_phrase}.",
        "{player_name} goes into the book for {team}. {score_phrase}.",
    ),
    "substitution": (
        "{player_on_name} replaces {player_off_name} for {team}. {score_phrase}.",
        "On comes {player_on_name} for {team}, with {player_off_name} making way. {score_phrase}.",
        "{team} turn to their bench: {player_on_name} on, {player_off_name} off. {score_phrase}.",
        "Fresh legs for {team}: {player_on_name} on for {player_off_name}. {score_phrase}.",
    ),
    # {back_line} is ", a back three now" when the first number of the formation changes and
    # empty otherwise (agents/wording.back_line_note).
    "formation_change": (
        "{team} move from a {previous_formation} to a {formation}{back_line}. {score_phrase}.",
        "{team} have changed to a {formation} shape{back_line}. {score_phrase}.",
        "{team} reorganise into a {formation}{back_line}. {score_phrase}.",
        "A change of shape from {team}, now set up in a {formation}{back_line}. {score_phrase}.",
    ),
    "big_chance": (
        "{chance_descriptor_title} for {team}. {score_phrase}.",
        "{player_name} has {chance_descriptor} for {team}. {score_phrase}.",
    ),
    "signal_red_card": (
        "{team} are down to {players_on_pitch} players after a red card. {score_phrase}.",
        "{team} must reorganise a player short. {score_phrase}.",
        "A player light now, {team} must find a new balance. {score_phrase}.",
    ),
    # A red card and the reshape it forces often reach the same tactical window; one line
    # then carries both beats.
    "signal_red_card_formation": (
        "{team} are down to {players_on_pitch} players and reorganise into a {new_formation}"
        "{back_line}. {score_phrase}.",
        "A player short, {team} switch from a {previous_formation} to a {new_formation}"
        "{back_line}. {score_phrase}.",
        "{team} must reorganise a player short, and they settle into a {new_formation}"
        "{back_line}. {score_phrase}.",
    ),
    "signal_formation_change": (
        "{team} move from a {previous_formation} to a {new_formation}{back_line}. {score_phrase}.",
        "{team} abandon the {previous_formation} and settle into a {new_formation}{back_line}. "
        "{score_phrase}.",
        "{team} have changed to a {new_formation} shape{back_line}. {score_phrase}.",
    ),
    # A comparison names the period it compares with ("up from 45% in the 10 minutes before
    # that"); no version restates a swing in points on its own.
    "possession_swing": (
        "{team} have had {after}% of the possession over the last {window} minutes. {score_phrase}.",
        # {window_words} is the configured window length in words ("ten").
        "{team} have taken over the ball: {after}% of the possession in the last "
        "{window_words} minutes, up from {before}% before that.",
        "{team} have taken control of the ball in this spell. {score_phrase}.",
        "{team} are seeing much more of the ball now. {score_phrase}.",
    ),
    "high_turnover_surge": (
        "{team} are winning the ball high up the pitch: {recent} high turnovers in this spell.",
        "{team} are pressing and it is working, with {recent} high turnovers recently.",
        "{team} keep winning it back in their opponents' half. {score_phrase}.",
        "Another turnover high up for {team}, and the press is biting. {score_phrase}.",
        "{team} are forcing mistakes high up the pitch. {score_phrase}.",
    ),
    "xg_surge": (
        "{team} are creating {chance_descriptor} in this spell. {score_phrase}.",
        "The openings are falling to {team} at the moment. {score_phrase}.",
        "{team} have had the better of the chances in this spell. {score_phrase}.",
        "{team} have the momentum, and the chances to go with it. {score_phrase}.",
        "{team} are carving out the clearer openings now. {score_phrase}.",
        "{team} are the side asking the questions in this spell. {score_phrase}.",
    ),
}


# Casual mode (profile.py): the story in words, at most one number in a line, and the score
# phrase counts as that one. Formations, counts and shares become words ("a change of shape",
# "a player short", "more of the ball"); the one template with a share leaves out the score.
# The analyst set is FALLBACK_VARIANTS above, which may use the numbers the facts give.
CASUAL_FALLBACK_VARIANTS: dict[FallbackKind, tuple[str, ...]] = {
    "goal": FALLBACK_VARIANTS["goal"],
    "red_card": FALLBACK_VARIANTS["red_card"],
    "booking": FALLBACK_VARIANTS["booking"],
    "substitution": FALLBACK_VARIANTS["substitution"],
    "formation_change": (
        "{team} have changed their shape. {score_phrase}.",
        "A change of shape from {team}. {score_phrase}.",
        "{team} reorganise, with a new shape. {score_phrase}.",
    ),
    "big_chance": FALLBACK_VARIANTS["big_chance"],
    "signal_red_card": (
        "{team} must reorganise a player short. {score_phrase}.",
        "A player light now, {team} must find a new balance. {score_phrase}.",
    ),
    "signal_red_card_formation": (
        "A player short, {team} change their shape. {score_phrase}.",
        "{team} must reorganise a player short, with a new shape. {score_phrase}.",
    ),
    "signal_formation_change": (
        "{team} have changed their shape. {score_phrase}.",
        "A change of shape from {team}. {score_phrase}.",
        "{team} reorganise, with a new shape. {score_phrase}.",
    ),
    "possession_swing": (
        "{team} have taken control of the ball in this spell. {score_phrase}.",
        "{team} are seeing much more of the ball now. {score_phrase}.",
        "{team} have had {after}% of the possession in this spell.",
    ),
    "high_turnover_surge": (
        "{team} keep winning it back in their opponents' half. {score_phrase}.",
        "Another turnover high up for {team}, and the press is biting. {score_phrase}.",
        "{team} are forcing mistakes high up the pitch. {score_phrase}.",
    ),
    "xg_surge": FALLBACK_VARIANTS["xg_surge"],
}
FallbackMode = str  # "casual" or "analyst"


def _variants(kind: FallbackKind, mode: FallbackMode | None) -> tuple[str, ...]:
    return (CASUAL_FALLBACK_VARIANTS if mode == "casual" else FALLBACK_VARIANTS)[kind]


_SCORE_SENTENCE = " {score_phrase}."
# Placeholders that may be empty: the variant is still usable without them.
_OPTIONAL = {"back_line": ""}


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def fallback_lines(
    kind: FallbackKind,
    values: Mapping[str, object],
    *,
    line_id: str,
    mode: FallbackMode | None = None,
) -> Iterator[str]:
    """Yield the usable variants for a kind, in an order seeded by the line ID."""
    for _, text in fallback_choices(kind, values, line_id=line_id, mode=mode):
        yield text


def fallback_choices(
    kind: FallbackKind,
    values: Mapping[str, object],
    *,
    line_id: str,
    used: Collection[str] = (),
    mode: FallbackMode | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield (template, text) for the usable variants, in an order seeded by the line ID.

    A variant is usable only when every placeholder it needs has a value. With no
    score_phrase, the line isn't meant to state the score, so the closing score sentence is
    dropped rather than the variant. Templates in ``used`` (this match's earlier fallback
    lines) come last, so no version repeats while an unused one can be said. ``mode``
    "casual" picks the casual set; anything else the analyst set.
    """
    for template, text in _ordered_choices(kind, values, line_id=line_id, mode=mode):
        if template not in used:
            yield template, text
    for template, text in _ordered_choices(kind, values, line_id=line_id, mode=mode):
        if template in used:
            yield template, text


def _ordered_choices(
    kind: FallbackKind,
    values: Mapping[str, object],
    *,
    line_id: str,
    mode: FallbackMode | None = None,
) -> Iterator[tuple[str, str]]:
    available = {
        **_OPTIONAL,
        **{key: value for key, value in values.items() if value not in (None, "")},
    }
    # (the template as written, which names it; the template as said)
    templates = [(template, template) for template in _variants(kind, mode)]
    if "score_phrase" not in available:
        templates = [
            (template, template.replace(_SCORE_SENTENCE, "")) for template, _ in templates
        ]
    usable = [
        (template, said)
        for template, said in templates
        if _placeholders(said) <= available.keys()
    ]
    random.Random(line_id).shuffle(usable)
    for template, said in usable:
        yield template, said.format(**available)
