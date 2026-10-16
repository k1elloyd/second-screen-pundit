"""Deterministic fallback lines, from the variants in prompts/commentator-persona.md.

A variant is picked at random, seeded by the line ID, so a replay always produces the same
line for the same moment. Each variant uses only placeholders filled from verified facts;
the caller still runs the evidence check and moves to the next variant if one fails.
"""

from __future__ import annotations

import random
import string
from collections.abc import Iterator, Mapping

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
    "formation_change": (
        "{team} move from a {previous_formation} to a {formation}. {score_phrase}.",
        "{team} have changed to a {formation} shape. {score_phrase}.",
        "{team} reorganise into a {formation}. {score_phrase}.",
        "A change of shape from {team}, now set up in a {formation}. {score_phrase}.",
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
        "{team} are down to {players_on_pitch} players and reorganise into a {new_formation}. "
        "{score_phrase}.",
        "A player short, {team} switch from a {previous_formation} to a {new_formation}. "
        "{score_phrase}.",
        "{team} must reorganise a player short, and they settle into a {new_formation}. "
        "{score_phrase}.",
    ),
    "signal_formation_change": (
        "{team} move from a {previous_formation} to a {new_formation}. {score_phrase}.",
        "{team} abandon the {previous_formation} and settle into a {new_formation}. {score_phrase}.",
        "{team} have changed to a {new_formation} shape. {score_phrase}.",
    ),
    "possession_swing": (
        "{team} have had {after}% of the possession over the last {window} minutes. {score_phrase}.",
        "{team} are seeing far more of the ball now: {after}% possession, up from {before}%.",
        "The ball belongs to {team} at the moment, their possession up by {change} points.",
        "{team} have taken control of the ball in this spell. {score_phrase}.",
    ),
    "high_turnover_surge": (
        "{team} are winning the ball high up the pitch: {recent} high turnovers in this spell.",
        "{team} are pressing and it is working, with {recent} high turnovers recently.",
        "{team} keep winning it back in their opponents' half. {score_phrase}.",
        "Another turnover high up for {team}, and the press is biting. {score_phrase}.",
    ),
    "xg_surge": (
        "{team} are creating {chance_descriptor} in this spell. {score_phrase}.",
        "The openings are falling to {team} at the moment. {score_phrase}.",
        "{team} have had the better of the expected goals over the last {window} minutes.",
        "{team} have the momentum, and the chances to go with it. {score_phrase}.",
    ),
}


_SCORE_SENTENCE = " {score_phrase}."


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def fallback_lines(
    kind: FallbackKind,
    values: Mapping[str, object],
    *,
    line_id: str,
) -> Iterator[str]:
    """Yield the usable variants for a kind, in an order seeded by the line ID.

    A variant is usable only when every placeholder it needs has a value. With no
    score_phrase, the line isn't meant to state the score, so the closing score sentence is
    dropped rather than the variant.
    """
    available = {key: value for key, value in values.items() if value not in (None, "")}
    templates = FALLBACK_VARIANTS[kind]
    if "score_phrase" not in available:
        templates = tuple(template.replace(_SCORE_SENTENCE, "") for template in templates)
    usable = [
        template
        for template in templates
        if _placeholders(template) <= available.keys()
    ]
    random.Random(line_id).shuffle(usable)
    for template in usable:
        yield template.format(**available)
