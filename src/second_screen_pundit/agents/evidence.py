"""Evidence validation with number extraction adapted from Brian Benz's MIT-licensed
grounding.py in bbenz/football-results-dashboard.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TypeAlias

from second_screen_pundit.agents.models import (
    CommentaryLine,
    DirectorDecision,
    Explanation,
    Insight,
    Recap,
)
from second_screen_pundit.agents.tools.analytics import Fact, ToolResult
from second_screen_pundit.engine.schema import Event, MatchInfo


_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>st|nd|rd|th|s)?(?![\w.]*\d)"
    r"(?:\s*(?P<scale>million|billion|thousand))?",
    re.IGNORECASE,
)
_SCORE = re.compile(r"(?<![\w\d-])(?P<home>\d{1,2})\s*[-–—]\s*(?P<away>\d{1,2})(?![-–—\w])")
_FORMATION = re.compile(r"\b\d{1,2}(?:-\d{1,2}){2,3}\b")
_BACK_LINE = re.compile(r"\bback\s+(?P<count>zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", re.IGNORECASE)
_SHIRT_NUMBER_PREFIX = re.compile(r"(?:\bNo\.\s*|#)\s*$", re.IGNORECASE)
_SCALES = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_NUMBER_WORD = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NumberMention:
    text: str
    value: float
    decimals: int
    scale: float = 1.0
    ordinal: bool = False


@dataclass(frozen=True)
class EvidenceReport:
    passed: bool
    failures: tuple[str, ...]


Narrative: TypeAlias = CommentaryLine | DirectorDecision | Insight | Explanation | Recap


def extract_numbers(text: str) -> list[NumberMention]:
    mentions: list[NumberMention] = []
    for match in _NUMBER.finditer(text):
        prefix = text[:match.start()]
        if _SHIRT_NUMBER_PREFIX.search(prefix):
            continue
        raw = match.group("num")
        decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
        scale = _SCALES.get((match.group("scale") or "").lower(), 1.0)
        value = float(raw.replace(",", "")) * scale
        if match.group("sign") in ("-", "−"):
            value = -value
        suffix = (match.group("suffix") or "").lower()
        mentions.append(
            NumberMention(
                text=match.group(0).strip(),
                value=value,
                decimals=decimals,
                scale=scale,
                ordinal=suffix in {"st", "nd", "rd", "th"},
            )
        )
    for match in _NUMBER_WORD.finditer(text):
        word = match.group(0).casefold()
        mentions.append(NumberMention(text=word, value=float(_NUMBER_WORDS[word]), decimals=0))
    return sorted(mentions, key=lambda mention: text.casefold().find(mention.text.casefold()))


def _narrative_parts(
    output: Narrative,
) -> list[tuple[str, list[str], list[str]]]:
    if isinstance(output, CommentaryLine):
        return [(output.text, output.fact_ids, output.event_ids)]
    if isinstance(output, DirectorDecision):
        return [(output.reason, output.fact_ids, output.event_ids)]
    if isinstance(output, Insight):
        return [(output.headline, output.fact_ids, output.event_ids)]
    if isinstance(output, Explanation):
        parts = [(output.answer, output.fact_ids, output.event_ids)]
        parts.extend(
            (claim.text, claim.fact_ids, claim.event_ids)
            for claim in output.claims
        )
        parts.extend((text, output.fact_ids, output.event_ids) for text in output.cannot_tell)
        if output.notice:
            parts.append((output.notice, output.fact_ids, output.event_ids))
        return parts
    parts = [
        (output.headline, output.fact_ids, output.event_ids),
        (output.summary, output.fact_ids, output.event_ids),
        (output.audio_script, output.fact_ids, output.event_ids),
    ]
    parts.extend(
        (moment.text, output.fact_ids, moment.event_ids)
        for moment in output.key_moments
    )
    if output.notice:
        parts.append((output.notice, output.fact_ids, output.event_ids))
    return parts


def _fact_number_values(fact: Fact) -> set[float]:
    value = fact.value
    if isinstance(value, bool):
        return set()
    if isinstance(value, int | float):
        return {abs(float(value))} if math.isfinite(float(value)) else set()
    if isinstance(value, str):
        return {abs(number.value) for number in extract_numbers(value)}
    return set()


def _team_name_mentioned(name: str, text: str) -> bool:
    variants = (name, re.sub(r"\s+FC$", "", name, flags=re.IGNORECASE))
    folded = text.casefold()
    return any(variant and variant.casefold() in folded for variant in variants)


def _matches_value(number: NumberMention, allowed: set[float]) -> bool:
    target = abs(number.value)
    if target in allowed:
        return True
    tolerance = 0.5 * (10 ** -number.decimals) * number.scale
    return any(abs(value - target) <= tolerance + 1e-9 for value in allowed)


def _score_matches(
    text: str,
    cited_facts: list[Fact],
    info: MatchInfo | None,
) -> list[str]:
    scores = [
        (fact, str(fact.value))
        for fact in cited_facts
        if fact.label.casefold() == "score" and isinstance(fact.value, str)
    ]
    failures: list[str] = []
    for match in _SCORE.finditer(text):
        found = (int(match.group("home")), int(match.group("away")))
        accepted = False
        for _, raw_score in scores:
            score_match = re.fullmatch(r"\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*", raw_score)
            if score_match is None:
                continue
            expected = (int(score_match.group(1)), int(score_match.group(2)))
            if found == expected:
                accepted = True
                break
            if (
                found == expected[::-1]
                and info is not None
                and expected[1] > expected[0]
                and _team_name_mentioned(info.away.name, text)
            ):
                accepted = True
                break
            if (
                found == expected[::-1]
                and info is not None
                and expected[0] > expected[1]
                and _team_name_mentioned(info.home.name, text)
            ):
                accepted = True
                break
        if not accepted:
            failures.append(f"score {match.group(0)!r} does not match a cited score fact")
    return failures


def _formation_matches(text: str, cited_facts: list[Fact]) -> list[str]:
    formations = [
        str(fact.value)
        for fact in cited_facts
        if "formation" in fact.label.casefold() and isinstance(fact.value, str)
    ]
    failures: list[str] = []
    for match in _FORMATION.finditer(text):
        if match.group(0) not in formations:
            failures.append(f"formation {match.group(0)!r} does not match a cited formation fact")
    for match in _BACK_LINE.finditer(text):
        raw_count = match.group("count").casefold()
        count = _NUMBER_WORDS.get(raw_count)
        if count is None:
            count = int(raw_count)
        if not any(formation.split("-", 1)[0] == str(count) for formation in formations):
            failures.append(f"{match.group(0)!r} does not match a cited formation fact")
    return failures


def check_evidence(
    output: Narrative,
    *,
    events: list[Event],
    as_of: int,
    tool_results: list[ToolResult],
    info: MatchInfo | None = None,
) -> EvidenceReport:
    if as_of < 0:
        raise ValueError("as_of must be a non-negative event sequence")

    event_by_id = {event.event_id: event for event in events}
    facts_by_id = {
        fact.fact_id: fact
        for result in tool_results
        for fact in result.facts
    }
    failures: list[str] = []

    for event_id in output.event_ids:
        event = event_by_id.get(event_id)
        if event is None:
            failures.append(f"unknown event_id: {event_id}")
        elif event.seq > as_of:
            failures.append(f"future event_id: {event_id} is after as_of {as_of}")

    for fact_id in output.fact_ids:
        if fact_id not in facts_by_id:
            failures.append(f"unknown fact_id: {fact_id}")

    if isinstance(output, CommentaryLine) and info is not None:
        for fact in facts_by_id.values():
            if fact.label.casefold() != "score" or not isinstance(fact.value, str):
                continue
            score_match = _SCORE.fullmatch(fact.value)
            if score_match is None:
                continue
            home_score = int(score_match.group("home"))
            away_score = int(score_match.group("away"))
            if away_score <= home_score:
                continue
            expected = (away_score, home_score)
            score_mentions = list(_SCORE.finditer(output.text))
            if not score_mentions:
                continue
            mentions_expected_score = any(
                (int(match.group("home")), int(match.group("away"))) == expected
                for match in score_mentions
            )
            if (
                fact.fact_id not in output.fact_ids
                or not _team_name_mentioned(info.away.name, output.text)
                or not mentions_expected_score
            ):
                failures.append(
                    "away-leading commentary must cite the score and state it from "
                    f"{info.away.name}'s perspective as {away_score}-{home_score}"
                )
            break

    if isinstance(output, Recap):
        for moment in output.key_moments:
            cited_events = [
                event_by_id[event_id]
                for event_id in moment.event_ids
                if event_id in event_by_id and event_by_id[event_id].seq <= as_of
            ]
            if not any(
                event.minute + event.added_minute == moment.minute
                for event in cited_events
            ):
                failures.append(
                    f"recap minute {moment.minute} does not match a cited event"
                )

    for text, fact_ids, event_ids in _narrative_parts(output):
        for fact_id in fact_ids:
            if fact_id not in facts_by_id:
                failures.append(f"unknown fact_id: {fact_id}")
        for event_id in event_ids:
            event = event_by_id.get(event_id)
            if event is None:
                failures.append(f"unknown event_id: {event_id}")
            elif event.seq > as_of:
                failures.append(f"future event_id: {event_id} is after as_of {as_of}")
        cited_facts = [facts_by_id[fact_id] for fact_id in fact_ids if fact_id in facts_by_id]
        allowed_numbers = {
            value
            for fact in cited_facts
            for value in _fact_number_values(fact)
        }
        for event_id in event_ids:
            event = event_by_id.get(event_id)
            if event is not None and event.seq <= as_of:
                allowed_numbers.update(
                    {float(event.minute), float(event.added_minute), float(event.minute + event.added_minute)}
                )

        for number in extract_numbers(text):
            if number.ordinal and 1 <= number.value <= 10:
                continue
            if not _matches_value(number, allowed_numbers):
                failures.append(f"ungrounded number {number.text!r}")
        failures.extend(_score_matches(text, cited_facts, info))
        failures.extend(_formation_matches(text, cited_facts))

    unique_failures = tuple(dict.fromkeys(failures))
    return EvidenceReport(passed=not unique_failures, failures=unique_failures)
