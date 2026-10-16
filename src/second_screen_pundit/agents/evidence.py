"""Evidence validation with number extraction adapted from Brian Benz's MIT-licensed
grounding.py in bbenz/football-results-dashboard.
"""

from __future__ import annotations

import math
import re
from collections import Counter
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


def _is_score_fact(fact: Fact) -> bool:
    return fact.label.casefold().startswith("match-level score")


def _team_name_mentioned(name: str, text: str) -> bool:
    variants = (name, re.sub(r"\s+FC$", "", name, flags=re.IGNORECASE))
    folded = text.casefold()
    return any(variant and variant.casefold() in folded for variant in variants)


def _matches_value(number: NumberMention, allowed: set[float]) -> bool:
    return abs(number.value) in allowed


def _metric_mentioned(fact: Fact, text: str) -> bool:
    label = fact.label.casefold()
    sentence = text.casefold()
    metrics = (
        (("shots on target", "on target"), ("on target",)),
        (("best chance",), ("best chance",)),
        (("progressive passes", "progressive pass"), ("progressive pass", "progressive passes")),
        (("passing pair",), ("passing pair", "passes")),
        (("defensive actions per minute",), ("defensive actions", "pressing")),
        (("high turnovers",), ("high turnover", "turnovers")),
        (("possession",), ("possession",)),
        (("shots", "shot"), ("shots", "shot", "attempts")),
        (("xg",), ("xg", "expected goals")),
        (("cards", "card"), ("cards", "card", "bookings")),
        (("players on the pitch",), ("players",)),
    )
    for label_terms, sentence_terms in metrics:
        if any(term in label for term in label_terms):
            return any(term in sentence for term in sentence_terms)
    return False


def _is_score_context(text: str) -> bool:
    return bool(re.search(r"\b(score|lead|leads|leading|ahead|behind|level|draw|nil)\b", text, re.I))


def _has_statistic_term(text: str) -> bool:
    return bool(
        re.search(
            r"\b(possession|shots?|on target|xg|expected goals|cards?|bookings|"
            r"turnovers?|pressing|defensive actions|passes|players)\b",
            text,
            re.I,
        )
    )


def _number_is_grounded(
    number: NumberMention,
    sentence: str,
    cited_facts: list[Fact],
    *,
    info: MatchInfo | None,
) -> bool:
    score_mentions = list(_SCORE.finditer(sentence))
    if any(
        match.start() <= sentence.casefold().find(number.text.casefold()) < match.end()
        for match in score_mentions
    ):
        return True

    metric_is_stated = _has_statistic_term(sentence)
    if _is_score_context(sentence) and not metric_is_stated:
        if any(
            _is_score_fact(fact) and _matches_value(number, _fact_number_values(fact))
            for fact in cited_facts
        ):
            return True

    for fact in cited_facts:
        if _is_score_fact(fact) or not _metric_mentioned(fact, sentence):
            continue
        values = _fact_number_values(fact)
        if (
            fact.label.casefold().startswith("match-level ")
            and not (
                info is not None
                and any(
                    _team_name_mentioned(name, sentence)
                    for name in (info.home.name, info.away.name)
                )
            )
            and re.search(r"\b(match|overall|total)\b", sentence, re.I)
            and _matches_value(number, values)
        ):
            return True
        if info is None:
            continue
        team_name = next(
            (
                name
                for name in (info.home.name, info.away.name)
                if fact.label.casefold().startswith(name.casefold())
            ),
            None,
        )
        if team_name is None or not _team_name_mentioned(team_name, sentence):
            continue
        if _matches_value(number, values):
            return True
    return False


def _score_matches(
    text: str,
    cited_facts: list[Fact],
    info: MatchInfo | None,
) -> list[str]:
    scores = [
        (fact, str(fact.value))
        for fact in cited_facts
        if _is_score_fact(fact) and isinstance(fact.value, str)
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


def _formation_matches(
    text: str,
    cited_facts: list[Fact],
    info: MatchInfo | None,
) -> list[str]:
    formations = [
        str(fact.value)
        for fact in cited_facts
        if "formation" in fact.label.casefold()
        and isinstance(fact.value, str)
        and info is not None
        and any(
            fact.label.casefold().startswith(name.casefold())
            and _team_name_mentioned(name, text)
            for name in (info.home.name, info.away.name)
        )
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
    all_fact_ids = [
        fact.fact_id
        for result in tool_results
        for fact in result.facts
    ]
    duplicate_ids = [
        fact_id for fact_id, count in Counter(all_fact_ids).items() if count > 1
    ]
    facts_by_id = {
        fact.fact_id: fact
        for result in tool_results
        for fact in result.facts
    }
    failures: list[str] = [
        f"duplicate fact_id within run: {fact_id}" for fact_id in duplicate_ids
    ]

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
            if not _is_score_fact(fact) or not isinstance(fact.value, str):
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
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            score_spans = list(_SCORE.finditer(sentence))
            formation_spans = list(_FORMATION.finditer(sentence))
            back_line_spans = list(_BACK_LINE.finditer(sentence))
            for number in extract_numbers(sentence):
                if number.ordinal and 1 <= number.value <= 10:
                    continue
                number_start = sentence.casefold().find(number.text.casefold())
                if any(match.start() <= number_start < match.end() for match in score_spans):
                    continue
                if any(match.start() <= number_start < match.end() for match in formation_spans):
                    continue
                if any(match.start() <= number_start < match.end() for match in back_line_spans):
                    continue
                if not _number_is_grounded(
                    number,
                    sentence,
                    cited_facts,
                    info=info,
                ):
                    failures.append(f"ungrounded number {number.text!r}")
        failures.extend(_score_matches(text, cited_facts, info))
        failures.extend(_formation_matches(text, cited_facts, info))

    unique_failures = tuple(dict.fromkeys(failures))
    return EvidenceReport(passed=not unique_failures, failures=unique_failures)
