"""Evidence validation adapted from Brian Benz's MIT-licensed grounding.py.

Upstream: https://github.com/bbenz/football-results-dashboard/blob/d313f3896bd307b1d12b2d7bec034bb359c04e05/demo/src/football_insights/agent/grounding.py
License: https://github.com/bbenz/football-results-dashboard/blob/d313f3896bd307b1d12b2d7bec034bb359c04e05/LICENSE
Local copy: third_party/bbenz_grounding/LICENSE.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
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
from second_screen_pundit.engine.teams import label_side, name_mentioned, team_name


_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>st|nd|rd|th|s)?(?![\w.]*\d)"
    r"(?:\s*(?P<scale>million|billion|thousand))?",
    re.IGNORECASE,
)
# A score never ends in a minute mark: 59-75' is a minute range.
_SCORE = re.compile(r"(?<![\w\d-])(?P<home>\d{1,2})\s*[-–—]\s*(?P<away>\d{1,2})(?![-–—\w'’])")
_LABEL_MINUTES = re.compile(r"(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?'")
_FORMATION = re.compile(r"\b\d{1,2}(?:-\d{1,2}){2,3}\b")
_BACK_LINE = re.compile(r"\bback\s+(?P<count>zero|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", re.IGNORECASE)
_XG_DECIMAL = re.compile(
    r"\b(?:xg|expected goals)\b[^.!?]{0,32}\b\d+\.\d+"
    r"|\b\d+\.\d+\b[^.!?]{0,32}\b(?:xg|expected goals)\b",
    re.IGNORECASE,
)
# A recap shows no raw xG: any number with two or more decimal places is one.
_LONG_DECIMAL = re.compile(r"\d\.\d{2,}")
_SHIRT_NUMBER_PREFIX =re.compile(r"(?:\bNo\.\s*|#)\s*$", re.IGNORECASE)
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
    # One entry per grounded mention (a number, score, formation or minute), in text order:
    # the IDs of the cited facts that ground it. Callers use it to cite only what was used.
    grounding: tuple[tuple[str, ...], ...] = ()


Narrative: TypeAlias = CommentaryLine | DirectorDecision | Insight | Explanation | Recap


def _extract_number_spans(text: str) -> list[tuple[NumberMention, int, int]]:
    mentions: list[tuple[NumberMention, int, int]] = []
    for match in _NUMBER.finditer(text):
        if _SHIRT_NUMBER_PREFIX.search(text[:match.start()]):
            continue
        raw = match.group("num")
        decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
        scale = _SCALES.get((match.group("scale") or "").lower(), 1.0)
        value = float(raw.replace(",", "")) * scale
        if match.group("sign") in ("-", "−"):
            value = -value
        suffix = (match.group("suffix") or "").lower()
        mentions.append(
            (
                NumberMention(
                text=match.group(0).strip(),
                value=value,
                decimals=decimals,
                scale=scale,
                ordinal=suffix in {"st", "nd", "rd", "th"},
                ),
                match.start(),
                match.end(),
            )
        )
    for match in _NUMBER_WORD.finditer(text):
        word = match.group(0).casefold()
        mentions.append(
            (
                NumberMention(text=word, value=float(_NUMBER_WORDS[word]), decimals=0),
                match.start(),
                match.end(),
            )
        )
    return sorted(mentions, key=lambda item: item[1])


def extract_numbers(text: str) -> list[NumberMention]:
    return [mention for mention, _, _ in _extract_number_spans(text)]


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
    """The exact value, plus the deterministic spoken rounding when the fact has one."""
    value = fact.value
    values: set[float] = set()
    if isinstance(value, bool):
        return values
    if isinstance(value, int | float):
        if math.isfinite(float(value)):
            values.add(abs(float(value)))
    elif isinstance(value, str):
        values.update(abs(number.value) for number in extract_numbers(value))
    if fact.spoken_value is not None and math.isfinite(float(fact.spoken_value)):
        values.add(abs(float(fact.spoken_value)))
    return values


_CLOCK_LABELS = ("match-level clock", "match-level minutes remaining")


def _is_clock_fact(fact: Fact) -> bool:
    return fact.label.casefold().startswith(_CLOCK_LABELS)


def _is_score_fact(fact: Fact) -> bool:
    return fact.label.casefold().startswith("match-level score")


def _team_name_mentioned(info: MatchInfo, side: str, text: str) -> bool:
    """Full name or the short form defined in engine/teams.py."""
    return name_mentioned(team_name(info, side), text)


def _label_team_mentioned(fact: Fact, text: str, info: MatchInfo | None) -> bool:
    """True when the fact's label names a team and that team is named in the text."""
    if info is None:
        return False
    side = label_side(fact.label, info)
    return side is not None and name_mentioned(team_name(info, side), text)


def _matches_value(number: NumberMention, allowed: set[float]) -> bool:
    return abs(number.value) in allowed


def _metric_mentioned(fact: Fact, text: str) -> bool:
    label = fact.label.casefold()
    sentence = text.casefold()
    metrics = (
        (("minutes remaining",), ("minutes left", "minutes remaining", "minutes to go", "minutes to play")),
        (("match-level clock",), ("minute",)),
        (("shots on target", "on target"), ("on target",)),
        (("best chance",), ("best chance",)),
        (("big chances",), ("big chances", "clear chances")),
        (("expected goals, rounded",), ("expected goals",)),
        (("progressive passes", "progressive pass"), ("progressive pass", "progressive passes")),
        (("passing pair",), ("passing pair", "passes")),
        (("defensive actions per minute",), ("defensive actions", "pressing")),
        (("high turnovers",), ("high turnover", "turnovers")),
        (("possession",), ("possession",)),
        (("shots", "shot"), ("shots", "shot", "attempts")),
        (("xg",), ("xg", "expected goals")),
        (("cards", "card"), ("cards", "card", "bookings")),
        (("players on the pitch",), ("players", "men")),
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


def _grounding_facts(
    number: NumberMention,
    sentence: str,
    cited_facts: list[Fact],
    *,
    info: MatchInfo | None,
) -> list[Fact]:
    """Every cited fact that grounds this number in this sentence; empty when none does."""
    metric_is_stated = _has_statistic_term(sentence)
    grounding: list[Fact] = []
    if _is_score_context(sentence) and not metric_is_stated:
        grounding.extend(
            fact
            for fact in cited_facts
            if _is_score_fact(fact) and _matches_value(number, _fact_number_values(fact))
        )

    for fact in cited_facts:
        if _is_score_fact(fact) or not _metric_mentioned(fact, sentence):
            continue
        values = _fact_number_values(fact)
        if _is_clock_fact(fact):
            if _matches_value(number, values):
                grounding.append(fact)
            continue
        if (
            fact.label.casefold().startswith("match-level ")
            and not (
                info is not None
                and any(
                    _team_name_mentioned(info, side, sentence)
                    for side in ("home", "away")
                )
            )
            and re.search(r"\b(match|overall|total)\b", sentence, re.I)
            and _matches_value(number, values)
        ):
            grounding.append(fact)
            continue
        if not _label_team_mentioned(fact, sentence, info):
            continue
        if _matches_value(number, values):
            grounding.append(fact)
    return grounding


_PERCENT_AFTER = re.compile(r"\s*(?:%|per\s*cent\b|percent\b)", re.IGNORECASE)
_MINUTE_WORD = re.compile(r"\bminutes?\b", re.IGNORECASE)
_MINUTE_MARK_AFTER = re.compile(r"(?:\s*[-–]\s*\d{1,3})?['’]")


def _is_minute_mention(sentence: str, number: NumberMention, number_end: int) -> bool:
    """27', either end of 59-75', 27th, or a number in a sentence that talks about the minute."""
    return (
        _MINUTE_MARK_AFTER.match(sentence, number_end) is not None
        or number.ordinal
        or _MINUTE_WORD.search(sentence) is not None
    )


def mentions_score(text: str) -> bool:
    """True when the text states a score such as 2-1; formations never match."""
    return _SCORE.search(text) is not None


def _score_matches(
    text: str,
    cited_facts: list[Fact],
    info: MatchInfo | None,
    grounding: list[tuple[str, ...]] | None = None,
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
        for score_fact, raw_score in scores:
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
                and _team_name_mentioned(info, "away", text)
            ):
                accepted = True
                break
            if (
                found == expected[::-1]
                and info is not None
                and expected[0] > expected[1]
                and _team_name_mentioned(info, "home", text)
            ):
                accepted = True
                break
        if not accepted:
            failures.append(f"score {match.group(0)!r} does not match a cited score fact")
        elif grounding is not None:
            grounding.append((score_fact.fact_id,))
    return failures


def _formation_matches(
    text: str,
    cited_facts: list[Fact],
    info: MatchInfo | None,
    grounding: list[tuple[str, ...]] | None = None,
) -> list[str]:
    formation_facts = [
        fact
        for fact in cited_facts
        if "formation" in fact.label.casefold()
        and isinstance(fact.value, str)
        and _label_team_mentioned(fact, text, info)
    ]
    failures: list[str] = []
    for match in _FORMATION.finditer(text):
        matched = tuple(
            fact.fact_id for fact in formation_facts if str(fact.value) == match.group(0)
        )
        if not matched:
            failures.append(f"formation {match.group(0)!r} does not match a cited formation fact")
        elif grounding is not None:
            grounding.append(matched)
    for match in _BACK_LINE.finditer(text):
        raw_count = match.group("count").casefold()
        count = _NUMBER_WORDS.get(raw_count)
        if count is None:
            count = int(raw_count)
        matched = tuple(
            fact.fact_id
            for fact in formation_facts
            if str(fact.value).split("-", 1)[0] == str(count)
        )
        if not matched:
            failures.append(f"{match.group(0)!r} does not match a cited formation fact")
        elif grounding is not None:
            grounding.append(matched)
    return failures


@dataclass(frozen=True)
class FailureReason:
    """Why one check failed: the rule broken and what broke it (a number, name or ID)."""

    rule: str
    subject: str | None
    detail: str


# Most specific first. Each pattern's "subject" group is the offending number, name or ID.
_FAILURE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (rule, re.compile(pattern, re.IGNORECASE))
    for rule, pattern in (
        ("number not grounded by a cited fact for the named team and metric",
         r"ungrounded number '(?P<subject>[^']+)'"),
        ("percentage without % or per cent", r"percentage '(?P<subject>[^']+)' must be spoken"),
        ("away leader's score not stated from their side", r"away-leading commentary"),
        ("score does not match the score fact", r"score '(?P<subject>[^']+)' does not match"),
        ("formation does not match a cited formation for the named team",
         r"formation '(?P<subject>[^']+)' does not match"),
        ("back line mentioned without a change in its number",
         r"'(?P<subject>[^']*back[^']*)': describe the back line"),
        ("back line does not match a cited formation",
         r"'(?P<subject>[^']*back[^']*)' does not match a cited formation"),
        ("xG decimal spoken", r"xG (?:or expected-goals )?decimals"),
        ("score stated when score_phrase was null", r"score_phrase was null"),
        ("clock stated when clock_allowed was false", r"clock_allowed was false"),
        ("headline missing the score phrase", r"headline must contain score_phrase"),
        ("recap minute not from a cited event", r"recap minute (?P<subject>\d+)"),
        ("future event cited", r"future event_id: (?P<subject>\S+)"),
        ("unknown event cited", r"unknown event_id: (?P<subject>\S+)"),
        ("unknown fact cited", r"unknown fact_id: (?P<subject>\S+)"),
        ("event not supplied to the agent",
         r"event_id (?P<subject>\S+) was not (?:present|returned)"),
        ("fact cited that was not in the Insight", r"cites a fact that was not in the Insight"),
        ("event cited that was not in the Insight", r"cites an event that was not in the Insight"),
        ("no event cited", r"must cite an event"),
        ("IDs or brackets in spoken text", r"must be plain spoken English"),
        ("line too long", r"text is (?P<subject>\d+) characters"),
        ("banned phrase", r"banned phrase (?P<subject>.+)"),
        ("duplicate fact ID", r"duplicate fact_id within run: (?P<subject>\S+)"),
    )
)


def classify_failure(failure: str) -> FailureReason:
    for rule, pattern in _FAILURE_RULES:
        match = pattern.search(failure)
        if match is not None:
            subject = match.groupdict().get("subject")
            return FailureReason(rule, subject, failure)
    return FailureReason("other", None, failure)


def check_evidence(
    output: Narrative,
    *,
    events: list[Event],
    as_of: int,
    tool_results: list[ToolResult],
    info: MatchInfo | None = None,
    input_event_ids: Sequence[str] = (),
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
    grounded: list[tuple[str, ...]] = []
    supplied_event_ids = set(input_event_ids) | {
        event_id
        for result in tool_results
        for fact in result.facts
        for event_id in fact.event_ids
    }

    for event_id in output.event_ids:
        event = event_by_id.get(event_id)
        if event is None:
            failures.append(f"unknown event_id: {event_id}")
        elif event.seq > as_of:
            failures.append(f"future event_id: {event_id} is after as_of {as_of}")
        elif event_id not in supplied_event_ids:
            failures.append(f"event_id {event_id} was not present in the Insight or tool results")

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
                or not _team_name_mentioned(info, "away", output.text)
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
            elif event_id not in supplied_event_ids:
                failures.append(
                    f"event_id {event_id} was not present in the Insight or tool results"
                )
        if isinstance(output, CommentaryLine) and _XG_DECIMAL.search(text):
            failures.append("CommentaryLine must not state xG or expected-goals decimals")
        if isinstance(output, Recap) and (_XG_DECIMAL.search(text) or _LONG_DECIMAL.search(text)):
            failures.append("recap must not state xG decimals")
        cited_facts = [facts_by_id[fact_id] for fact_id in fact_ids if fact_id in facts_by_id]
        # Answers and recaps may give the minute of an event they cite (spec: "or the minute
        # of a cited event"), or a minute in a cited fact's label, such as its window
        # 59-75'. Live lines keep the stricter clock-fact rule.
        # Each cited minute maps to the cited facts that carry it (through an event or the
        # label); a minute from an event ID cited directly maps to no fact.
        cited_minutes: dict[float, list[str]] = {}
        if isinstance(output, Explanation | Recap):
            for event_id in event_ids:
                event = event_by_id.get(event_id)
                if event is not None and event.seq <= as_of:
                    cited_minutes.setdefault(float(event.minute + event.added_minute), [])
            for fact in cited_facts:
                for event_id in fact.event_ids:
                    event = event_by_id.get(event_id)
                    if event is not None and event.seq <= as_of:
                        cited_minutes.setdefault(
                            float(event.minute + event.added_minute), []
                        ).append(fact.fact_id)
                for match in _LABEL_MINUTES.finditer(fact.label):
                    for minute in match.groups():
                        if minute is not None:
                            cited_minutes.setdefault(float(minute), []).append(fact.fact_id)
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            score_spans = list(_SCORE.finditer(sentence))
            formation_spans = list(_FORMATION.finditer(sentence))
            back_line_spans = list(_BACK_LINE.finditer(sentence))
            for number, number_start, number_end in _extract_number_spans(sentence):
                if number.ordinal and 1 <= number.value <= 10:
                    continue
                if any(
                    number_start < span.end() and number_end > span.start()
                    for span in [*score_spans, *formation_spans, *back_line_spans]
                ):
                    continue
                if number.value in cited_minutes and _is_minute_mention(
                    sentence, number, number_end
                ):
                    grounded.append(tuple(dict.fromkeys(cited_minutes[number.value])))
                    continue
                grounding = _grounding_facts(number, sentence, cited_facts, info=info)
                grounded.append(tuple(fact.fact_id for fact in grounding))
                if not grounding:
                    failures.append(f"ungrounded number {number.text!r}")
                elif all(fact.unit == "%" for fact in grounding) and not _PERCENT_AFTER.match(
                    sentence, number_end
                ):
                    failures.append(
                        f"percentage {number.text!r} must be spoken with % or per cent"
                    )
        failures.extend(_score_matches(text, cited_facts, info, grounded))
        failures.extend(_formation_matches(text, cited_facts, info, grounded))

    unique_failures = tuple(dict.fromkeys(failures))
    return EvidenceReport(
        passed=not unique_failures,
        failures=unique_failures,
        grounding=tuple(group for group in grounded if group),
    )
