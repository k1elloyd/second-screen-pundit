"""Polish session, group 1: how lines are worded (clock, formations, team names)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.fallbacks import FALLBACK_VARIANTS, fallback_lines
from second_screen_pundit.agents.models import CommentaryLine, CommentaryReply
from second_screen_pundit.agents.pipeline import LiveWindowResult, PunditPipeline
from second_screen_pundit.agents.prompts import COMMENTATOR_INSTRUCTIONS
from second_screen_pundit.agents.signals import TacticalSignal
from second_screen_pundit.agents.tools.analytics import formation_at, match_state
from second_screen_pundit.agents.tools.parameters import FormationAtParams, MatchStateParams
from second_screen_pundit.agents.wording import (
    apply_clock_rules,
    back_line_failures,
    back_line_note,
    banned_phrase_hits,
    clock_budget_allows,
    mentions_clock,
    mentions_minute,
    mentions_minutes_left,
    name_teams,
    strip_leading_clock,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event
from second_screen_pundit.engine.teams import SHORT_NAMES, team_name


def _settings() -> AgentSettings:
    return AgentSettings(
        mode="live",
        endpoint=None,
        model_fast=None,
        model_deep=None,
        speech_endpoint=None,
        speech_region=None,
        live_timeout_seconds=15,
        explainer_timeout_seconds=20,
        recap_timeout_seconds=45,
        importance_threshold=0.45,
        cooldown_match_minutes=4,
    )


def _scripted(
    monkeypatch: pytest.MonkeyPatch, reply: Callable[[dict[str, object]], str]
) -> tuple[PunditPipeline, list[dict[str, object]]]:
    """A pipeline whose commentator writes ``reply(prompt)`` and cites the whole insight."""
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    prompts: list[dict[str, object]] = []

    class Agent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            prompts.append(payload)
            return SimpleNamespace(
                value=CommentaryReply(
                    text=reply(payload),
                    event_ids=payload["insight"]["event_ids"],  # type: ignore[index]
                    fact_ids=payload["insight"]["fact_ids"],  # type: ignore[index]
                )
            )

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Agent())
    return pipeline, prompts


def _first(match_id: str, kind: str, **detail: object) -> tuple[list[Event], Event]:
    events = generate_match(match_id)
    event = next(
        event
        for event in events
        if event.type == kind and all(event.detail.get(k) == v for k, v in detail.items())
    )
    return events, event


# Clock


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("At 21 minutes, Ashbridge Rovers press high.", "Ashbridge Rovers press high."),
        ("At minute 72, Port Calder lead 2-0.", "Port Calder lead 2-0."),
        ("In the 72nd minute, Ashbridge go long.", "Ashbridge go long."),
        ("Port Calder lead 1-0. At minute 30, they sit deep.", "Port Calder lead 1-0. They sit deep."),
    ],
)
def test_a_leading_clock_is_removed_not_rejected(text: str, expected: str) -> None:
    assert strip_leading_clock(text) == expected


def test_a_line_with_the_time_left_does_not_also_give_the_minute() -> None:
    text = "Ashbridge Rovers go to a 3-4-3 at minute 72, with 18 minutes left."
    polished = apply_clock_rules(text, clock_allowed=True)
    assert polished == "Ashbridge Rovers go to a 3-4-3, with 18 minutes left."
    assert mentions_minutes_left(polished) and not mentions_minute(polished)


def test_without_a_clock_budget_every_clock_reading_is_removed() -> None:
    for text in (
        "Ashbridge Rovers go to a 3-4-3 with 18 minutes left.",
        "With 24 minutes left, Port Calder sit deep.",
        "Port Calder sit deep at 66 minutes.",
        "Eighteen minutes left, and Ashbridge Rovers push on.",
        # Seen in the live gate: a formation line ending "67 minutes gone."
        "Ashbridge Rovers switch to a 4-2-3-1, as they look for a way back. 67 minutes gone.",
        "Ashbridge Rovers switch to a 4-2-3-1, 67 minutes played.",
    ):
        assert not mentions_clock(apply_clock_rules(text, clock_allowed=False)), text


def test_a_window_length_is_not_the_clock() -> None:
    assert not mentions_clock(
        "Ashbridge Rovers have had 53% of the possession over the last 10 minutes."
    )


def test_clock_budget_is_one_line_in_four() -> None:
    assert not clock_budget_allows([])
    assert clock_budget_allows(["a.", "b.", "c."])
    assert not clock_budget_allows(["a.", "b.", "At minute 9, c.", "d.", "e."])
    lines: list[str] = []
    for index in range(40):
        lines.append("Late on, with 9 minutes left." if clock_budget_allows(lines) else f"L{index}.")
    used = sum(mentions_clock(line) for line in lines)
    assert 0 < used and used * 4 <= len(lines)


def test_model_line_opening_with_the_clock_is_kept_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, _ = _scripted(
        monkeypatch,
        lambda p: f"At 23 minutes, Brio Fallowmere strikes first. {p['score_phrase']}.",
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert result.line is not None and result.line.source == "agent"
    assert result.line.text == "Brio Fallowmere strikes first. Port Calder FC lead 1-0."


def test_no_clock_budget_means_no_clock_facts_and_no_clock_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: f"Brio Fallowmere strikes first, with 67 minutes left. {p['score_phrase']}.",
    )
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq, clock_allowed=False)
    )
    assert prompts[0]["clock_allowed"] is False
    labels = [str(fact["label"]) for fact in prompts[0]["facts"]]  # type: ignore[union-attr]
    assert not any(label.startswith("Match-level clock") for label in labels)
    assert not any(label.startswith("Match-level minutes remaining") for label in labels)
    assert result.line is not None and result.line.source == "agent"
    assert not mentions_clock(result.line.text)


def test_routine_lines_never_get_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, substitution = _first("m01", "substitution")
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: "At minute 60, a change for Ashbridge Rovers, with 30 minutes left.",
    )
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [substitution], as_of=substitution.seq)
    )
    assert prompts[0]["clock_allowed"] is False
    assert result.line is not None and not mentions_clock(result.line.text)


def test_commentator_instructions_ration_the_clock() -> None:
    assert "clock_allowed" in COMMENTATOR_INSTRUCTIONS
    assert "never both" in COMMENTATOR_INSTRUCTIONS
    assert "At 21 minutes" in COMMENTATOR_INSTRUCTIONS


class _ClockyPipeline:
    """Always writes a clock line, ignoring the budget: the dispatcher must still hold it."""

    async def comment_on_window(
        self,
        info: object,
        events: list[Event],
        window: Sequence[Event],
        *,
        as_of: int,
        signals: Sequence[TacticalSignal] = (),
        is_stale: Callable[[], bool] | None = None,
        recent_lines: Sequence[str] = (),
        clock_allowed: bool = True,
        late_clock_allowed: bool = False,
        used_fallbacks: set[str] | None = None,
    ) -> LiveWindowResult:
        line = CommentaryLine(
            line_id=f"x-l{as_of}",
            text=f"Ashbridge Rovers push on at minute {window[-1].minute}, with 9 minutes left.",
            insight_id="i",
            tone="measured",
            event_ids=[window[-1].event_id],
        )
        return LiveWindowResult("commentary", line, None, (), None)


async def _feed(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        yield event
        await asyncio.sleep(0)


@pytest.mark.parametrize("match_id", ["m01", "m02"])
def test_published_lines_mention_the_clock_at_most_one_in_four(match_id: str) -> None:
    info, events = get_match_info(match_id), generate_match(match_id)
    dispatcher = LiveDispatcher(_ClockyPipeline(), info, events, _settings())  # type: ignore[arg-type]
    asyncio.run(dispatcher.run(_feed(events)))
    lines = [
        outcome.result.line.text
        for outcome in dispatcher.outcomes
        if outcome.result is not None and outcome.result.line is not None
    ]
    clock_lines = [line for line in lines if mentions_clock(line)]
    assert len(lines) >= 8
    assert len(clock_lines) * 4 <= len(lines), lines
    assert not [line for line in lines if mentions_minute(line) and mentions_minutes_left(line)]


# Formations


def _formation_values(previous: str, new: str) -> dict[str, object]:
    return {
        "team": "Ashbridge Rovers",
        "formation": new,
        "new_formation": new,
        "previous_formation": previous,
        "players_on_pitch": 10,
        "score_phrase": "Port Calder lead 2-0",
        "back_line": back_line_note(previous, new),
    }


@pytest.mark.parametrize("kind", ["formation_change", "signal_formation_change", "signal_red_card_formation"])
def test_back_line_is_mentioned_only_when_its_number_changes(kind: str) -> None:
    unchanged = list(fallback_lines(kind, _formation_values("4-2-3-1", "4-3-3"), line_id="x"))
    changed = list(fallback_lines(kind, _formation_values("4-2-3-1", "3-4-3"), line_id="x"))
    assert len(unchanged) == len(changed) == len(FALLBACK_VARIANTS[kind])
    assert not [line for line in unchanged if re.search(r"\bback (four|three|five)\b", line)]
    assert all("back three" in line for line in changed), changed


def test_model_back_line_is_checked_against_the_formation_change() -> None:
    assert back_line_failures(
        "Ashbridge Rovers keep a back four in a 4-3-3.", [("4-2-3-1", "4-3-3")]
    )
    assert not back_line_failures(
        "Ashbridge Rovers go to a 3-4-3, a back three now.", [("4-2-3-1", "3-4-3")]
    )
    assert back_line_failures("Ashbridge Rovers hold a back four.", [])


def test_m01_back_three_fallback_names_the_back_three(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, change = _first("m01", "formation_change", formation="3-4-3")
    _, earlier = _first("m01", "formation_change", formation="4-2-3-1")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Failing:
        async def run(self, prompt: str) -> SimpleNamespace:
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Failing())
    back_three = asyncio.run(pipeline.comment_on_window(info, events, [change], as_of=change.seq))
    before = asyncio.run(pipeline.comment_on_window(info, events, [earlier], as_of=earlier.seq))
    assert back_three.line is not None and back_three.line.source == "fallback"
    assert "back three" in back_three.line.text
    assert before.line is not None and "back " not in before.line.text


def test_model_back_four_without_a_back_line_change_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, change = _first("m01", "formation_change", formation="4-2-3-1")
    previous = formation_at(
        events, FormationAtParams(team="home", minute=change.minute), as_of=change.seq - 1
    ).facts[0].value
    assert str(previous).split("-")[0] == "4"
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: (
            f"Ashbridge Rovers switch to a 4-2-3-1, still a back four. {p['score_phrase']}."
            if not p["retry_feedback"]
            else f"Ashbridge Rovers switch to a 4-2-3-1. {p['score_phrase']}."
        ),
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [change], as_of=change.seq))
    assert len(prompts) == 2 and "back line" in str(prompts[1]["retry_feedback"])
    assert result.line is not None and result.line.source == "agent"
    assert "back four" not in result.line.text


# Team names


def test_every_team_has_a_short_form_defined_in_teams_py() -> None:
    squads = yaml.safe_load(
        Path("src/second_screen_pundit/engine/squads.yaml").read_text(encoding="utf-8")
    )
    names = [team["name"] for team in squads["teams"]]
    assert sorted(names) == sorted(SHORT_NAMES)
    assert SHORT_NAMES["Port Calder FC"] == "Port Calder"
    assert SHORT_NAMES["Ashbridge Rovers"] == "Ashbridge"


def test_a_team_named_twice_is_named_in_full_then_short() -> None:
    info = get_match_info("m01")
    text = name_teams("Ashbridge Rovers go to a 3-4-3. Ashbridge Rovers lead 3-2.", info)
    assert text == "Ashbridge Rovers go to a 3-4-3. Ashbridge lead 3-2."
    assert text.index("Ashbridge Rovers") < text.index("Ashbridge lead")


def test_a_short_form_can_be_two_words_and_fc_goes_after_the_first_mention() -> None:
    info = get_match_info("m02")
    text = name_teams(
        "Port Calder lead 1-0, and Port Calder FC sit deep against Merehaven Athletic. "
        "Merehaven Athletic press.",
        info,
    )
    assert text == (
        "Port Calder FC lead 1-0, and Port Calder sit deep against Merehaven Athletic. "
        "Merehaven press."
    )
    assert text.count("FC") == 1


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_evidence_check_accepts_each_teams_short_form(match_id: str) -> None:
    info, events = get_match_info(match_id), generate_match(match_id)
    as_of = events[-1].seq
    for side in ("home", "away"):
        short = team_name(info, side).short
        result = formation_at(events, FormationAtParams(team=side, minute=90), as_of=as_of)
        fact = result.facts[0]
        line = CommentaryLine(
            line_id="x",
            text=f"{short} finish in a {fact.value}.",
            insight_id="i",
            tone="measured",
            fact_ids=[fact.fact_id],
            event_ids=fact.event_ids[:1],
        )
        report = check_evidence(line, events=events, as_of=as_of, tool_results=[result], info=info)
        assert report.passed, (short, report.failures)
    state = match_state(events, MatchStateParams(minute=90), as_of=as_of)
    score = next(fact for fact in state.facts if fact.label.startswith("Match-level score"))
    home, away = (int(value) for value in str(score.value).split("-"))
    leader = "home" if home > away else "away"
    shown = f"{max(home, away)}-{min(home, away)}"
    line = CommentaryLine(
        line_id="x",
        text=f"{team_name(info, leader).short} lead {shown}.",
        insight_id="i",
        tone="measured",
        fact_ids=[score.fact_id],
        event_ids=score.event_ids[:1],
    )
    report = check_evidence(line, events=events, as_of=as_of, tool_results=[state], info=info)
    assert report.passed, report.failures


def _assert_full_then_short(text: str, info: object) -> None:
    for side in ("home", "away"):
        name = team_name(info, side)  # type: ignore[arg-type]
        mentions = re.findall(
            rf"(?<!\w)(?:{re.escape(name.full)}|{re.escape(name.short)})(?!\w)", text
        )
        if mentions:
            assert mentions[0] == name.full, (text, mentions)
            assert all(mention == name.short for mention in mentions[1:]), (text, mentions)


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_recap_names_each_team_in_full_once_then_short(match_id: str) -> None:
    info, events = get_match_info(match_id), generate_match(match_id)
    pipeline = PunditPipeline(replace(_settings(), mode="off"), None)
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))
    assert result.evidence.passed, result.evidence.failures
    recap = result.recap
    for text in (recap.headline, recap.summary, recap.audio_script):
        _assert_full_then_short(text, info)


def test_live_fallback_lines_name_the_team_in_full_then_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m02")
    events, red = _first("m02", "card", colour="red")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Failing:
        async def run(self, prompt: str) -> SimpleNamespace:
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Failing())
    result = asyncio.run(pipeline.comment_on_window(info, events, [red], as_of=red.seq))
    assert result.line is not None and result.line.source == "fallback"
    _assert_full_then_short(result.line.text, info)


# Banned phrases


def _goal_sides(events: list[Event], as_of: int) -> list[str]:
    return [str(event.team) for event in events if event.type == "goal" and event.seq <= as_of]


def test_comeback_is_allowed_for_merehaven_after_their_equaliser_in_m02() -> None:
    info, events = get_match_info("m02"), generate_match("m02")
    equaliser = next(e for e in events if e.type == "goal" and e.minute == 54)
    text = "Merehaven Athletic complete the comeback. The score is level at 1-1."
    assert banned_phrase_hits(text, info, _goal_sides(events, equaliser.seq)) == []
    # Before the equaliser Merehaven are still behind, and Port Calder never trailed.
    assert banned_phrase_hits(text, info, _goal_sides(events, equaliser.seq - 1))
    assert banned_phrase_hits(
        "A Port Calder FC turnaround.", info, _goal_sides(events, equaliser.seq)
    )


def test_comeback_is_banned_at_nil_nil() -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    first_goal = next(e for e in events if e.type == "goal")
    hits = banned_phrase_hits(
        "Ashbridge Rovers need a comeback.", info, _goal_sides(events, first_goal.seq - 1)
    )
    assert hits == ["comeback: comeback"]


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("An INCREDIBLE finish from Juno Kestrelane.", "hype: incredible"),
        ("The odds are shifting.", "betting: odds"),
        ("VAR has a look.", "invented detail: VAR"),
        ("Ashbridge Rovers may have the winner.", "live only: the winner"),
        ("And Port Calder FC break.", "opener: and"),
        ("The visitors sit deep.", "opener: the visitors"),
    ],
)
def test_banned_phrases_are_whole_word_and_case_insensitive(text: str, hit: str) -> None:
    assert hit in banned_phrase_hits(text, get_match_info("m01"), [])


@pytest.mark.parametrize(
    "text",
    [
        "Ashbridge Rovers are better on the ball.",  # bet
        "The referee waves play on.",  # the referee is not banned
        "It's Ashbridge Rovers pressing now.",  # It's is not a banned opener
        "Port Calder FC defend the visitors' end.",  # the visitors, not at the start
        "Port Calder FC stand firm, and the press eases.",  # and, not at the start
    ],
)
def test_allowed_wording_is_not_caught(text: str) -> None:
    assert banned_phrase_hits(text, get_match_info("m01"), []) == []


def test_a_banned_model_line_falls_back_to_the_deterministic_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, prompts = _scripted(
        monkeypatch, lambda p: f"An incredible strike. {p['score_phrase']}."
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert len(prompts) == 1
    assert result.line is not None and result.line.source == "fallback"
    assert "incredible" not in result.line.text.casefold()
    assert result.banned_phrases == ("hype: incredible",)


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_every_cached_line_passes_the_banned_phrase_and_evidence_checks(match_id: str) -> None:
    from second_screen_pundit.agents.cache import load_cached_match
    from second_screen_pundit.agents.pipeline import _cached_tool_results

    info, events = get_match_info(match_id), generate_match(match_id)
    seq = {event.event_id: event.seq for event in events}
    cached = load_cached_match(match_id, cache_dir=Path("data/cached"))
    for line in cached.commentary:
        as_of = max(seq[event_id] for event_id in line.event_ids)
        published = line.model_copy(update={"text": name_teams(line.text, info)})
        assert banned_phrase_hits(published.text, info, _goal_sides(events, as_of)) == [], (
            published.text
        )
        report = check_evidence(
            published,
            events=events,
            as_of=as_of,
            tool_results=_cached_tool_results(events, as_of=as_of, fact_ids=line.fact_ids),
            info=info,
            input_event_ids=line.event_ids,
        )
        assert report.passed, (published.text, report.failures)
    recap = cached.recap
    for text in (recap.headline, recap.summary, recap.audio_script):
        assert banned_phrase_hits(text, info, _goal_sides(events, events[-1].seq)) == [], text


_NICKNAMES = re.compile(r"\b(?:the home side|the visitors|the hosts|the home team)\b", re.IGNORECASE)


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_cached_recaps_and_answers_use_team_names_full_then_short(match_id: str) -> None:
    from second_screen_pundit.agents.cache import load_cached_match

    info, events = get_match_info(match_id), generate_match(match_id)
    cached = load_cached_match(match_id, cache_dir=Path("data/cached"))
    recap = cached.recap
    texts = [
        recap.headline,
        recap.summary,
        recap.audio_script,
        *(moment.text for moment in recap.key_moments),
        *(question.answer for question in cached.questions),
    ]
    for text in texts:
        assert not _NICKNAMES.search(text), text
        assert banned_phrase_hits(text, info, _goal_sides(events, events[-1].seq)) == [], text
        _assert_full_then_short(text, info)

    pipeline = PunditPipeline(replace(_settings(), mode="cached"), None)
    as_of = events[-1].seq
    assert asyncio.run(pipeline.produce_recap(info, events, as_of=as_of)).source == "cached"
    for question in cached.questions:
        result = asyncio.run(pipeline.answer_question(info, events, question.question, as_of=as_of))
        assert result.source == "cached", (question.question, result.evidence.failures)
        assert result.explanation.answer == question.answer
