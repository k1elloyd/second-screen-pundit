"""Comparisons in words, checked against the match.

The evidence check grounds numbers. A line or an answer can also compare the teams without
one ("your side have had more of the ball", "Port Calder are under pressure"), and in casual
mode it is asked to. This module checks those comparisons against possession, shots and
chance quality (xG), computed from the events the viewer has seen.

A phrase belongs to the nearest team named before it in its sentence, the same rule as the
scorer-phrase check; "your side" and "your team" stand for My team. A comparison passes when
it is true for the match so far or for the current spell, and fails only when it is false for
both. The current spell is the last 10 match minutes (the live tactical signals' window) or
the last 15 (the Q&A snapshot's), so a claim drawn from either set of facts is judged on the
same footing. A "not" or "n't" between the team and the phrase reverses it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name

Metric = Literal["possession", "shots", "xg"]
Side = Literal["home", "away"]
# Match minutes for "the current spell": the tactical signals' window and the Q&A snapshot's.
SPELL_MINUTES = (10, 15)

# phrase -> (metric, or None for "any of the three"; True when the named team leads it)
_PHRASES: tuple[tuple[str, Metric | None, bool], ...] = (
    (r"more of the ball", "possession", True),
    (r"most of the ball", "possession", True),
    (r"more (?:of the )?possession", "possession", True),
    (r"(?:the )?edge in possession", "possession", True),
    (r"less of the ball", "possession", False),
    (r"less (?:of the )?possession", "possession", False),
    (r"more shots", "shots", True),
    (r"more attempts", "shots", True),
    (r"fewer shots", "shots", False),
    (r"fewer attempts", "shots", False),
    (r"(?:the )?better of the chances", "xg", True),
    (r"(?:the )?(?:clearer|better) (?:chances|openings)", "xg", True),
    (r"on top", None, True),
    (r"dominat(?:e|es|ed|ing)", None, True),
    (r"dominant", None, True),
    (r"under (?:the )?pressure", None, False),
    (r"under the cosh", None, False),
    (r"pinned back", None, False),
    (r"pressed back", None, False),
    (r"on the back foot", None, False),
)
_PATTERNS = [
    (phrase, re.compile(rf"(?<!\w){phrase}(?!\w)", re.IGNORECASE), metric, leads)
    for phrase, metric, leads in _PHRASES
]
_FRAMING = re.compile(r"\byour (?:side|team)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:not|never|no longer)\b|n['’]t\b", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class TeamTotals:
    """One period's possession share (successful passes), shots and xG, per side."""

    passes: dict[str, int]
    shots: dict[str, int]
    xg: dict[str, float]

    def value(self, metric: Metric, side: str) -> float:
        if metric == "possession":
            total = sum(self.passes.values())
            return self.passes[side] / total if total else 0.5
        if metric == "shots":
            return float(self.shots[side])
        return self.xg[side]


def _totals(events: Sequence[Event]) -> TeamTotals:
    passes = {"home": 0, "away": 0}
    shots = {"home": 0, "away": 0}
    xg = {"home": 0.0, "away": 0.0}
    for event in events:
        if event.team not in ("home", "away"):
            continue
        if event.type == "pass" and event.outcome == "success":
            passes[event.team] += 1
        elif event.type == "shot":
            shots[event.team] += 1
            xg[event.team] += event.xg or 0.0
    return TeamTotals(passes, shots, xg)


def match_periods(events: Sequence[Event], as_of: int) -> list[tuple[str, TeamTotals]]:
    """The match so far and each current spell, from the events up to ``as_of``."""
    visible = [event for event in events if event.seq <= as_of]
    if not visible:
        return []
    clock = max(event.minute + event.added_minute for event in visible)
    periods = [("the match so far", _totals(visible))]
    for minutes in SPELL_MINUTES:
        spell = [e for e in visible if e.minute + e.added_minute > clock - minutes]
        periods.append((f"the last {minutes} minutes", _totals(spell)))
    return periods


def _team_mentions(sentence: str, info: MatchInfo, my_team: str | None) -> list[tuple[int, int, str]]:
    """(start, end, side) for each team named in the sentence, "your side" included."""
    spans: list[tuple[int, int, str]] = []
    for side in ("home", "away"):
        for form in team_name(info, side).forms:
            pattern = re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", re.IGNORECASE)
            spans.extend((m.start(), m.end(), side) for m in pattern.finditer(sentence))
    if my_team in ("home", "away"):
        spans.extend((m.start(), m.end(), my_team) for m in _FRAMING.finditer(sentence))
    kept: list[tuple[int, int, str]] = []
    for start, end, side in sorted(spans, key=lambda item: (item[0], -item[1])):
        if any(start >= a and end <= b for a, b, _ in kept):
            continue  # a short form inside a full name is the same mention
        kept.append((start, end, side))
    return kept


@dataclass(frozen=True)
class Comparison:
    phrase: str
    team: str
    metric: Metric | None
    leads: bool


def find_comparisons(text: str, info: MatchInfo, my_team: str | None = None) -> list[Comparison]:
    """Each comparison phrase attached to a team, with negation applied."""
    found: list[Comparison] = []
    for sentence in _SENTENCE.split(text):
        teams = _team_mentions(sentence, info, my_team)
        for _, pattern, metric, leads in _PATTERNS:
            for match in pattern.finditer(sentence):
                before = [item for item in teams if item[1] <= match.start()]
                if not before:
                    continue  # no team to attach it to: nothing to check
                start, end, side = before[-1]
                negated = _NEGATION.search(sentence[end:match.start()]) is not None
                found.append(Comparison(match.group(0), side, metric, leads != negated))
    return found


def _holds(comparison: Comparison, totals: TeamTotals) -> bool:
    side = comparison.team
    other = "away" if side == "home" else "home"
    metrics: tuple[Metric, ...] = (
        (comparison.metric,) if comparison.metric else ("possession", "shots", "xg")
    )
    if comparison.leads:
        return any(totals.value(m, side) > totals.value(m, other) for m in metrics)
    return any(totals.value(m, side) < totals.value(m, other) for m in metrics)


def comparison_failures(
    text: str,
    info: MatchInfo,
    events: Sequence[Event],
    as_of: int,
    *,
    my_team: str | None = None,
) -> list[str]:
    """One failure per comparison that is false for the match so far and every current spell."""
    periods = match_periods(events, as_of)
    if not periods:
        return []
    failures = []
    for comparison in find_comparisons(text, info, my_team):
        if any(_holds(comparison, totals) for _, totals in periods):
            continue
        name = team_name(info, comparison.team).full
        what = comparison.metric or "possession, shots or chance quality"
        failures.append(
            f"comparison {comparison.phrase!r} for {name} is not true of {what} for the match "
            "so far or the current spell; say it the other way round or leave it out"
        )
    return failures
