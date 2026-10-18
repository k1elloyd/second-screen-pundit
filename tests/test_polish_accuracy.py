"""Polish follow-up: wording accuracy (scoring team, phase words, comparisons, variety)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents.cache import load_cached_match
from second_screen_pundit.agents.fallbacks import FALLBACK_VARIANTS, fallback_choices
from second_screen_pundit.agents.pipeline import PunditPipeline, score_phrase_allowed
from second_screen_pundit.agents.prompts import COMMENTATOR_INSTRUCTIONS
from second_screen_pundit.agents.signals import SignalTracker
from second_screen_pundit.agents.wording import (
    apply_phase_rules,
    banned_phrase_hits,
    clock_budget_allows,
    comparison_failures,
    end_with_punctuation,
    fix_possessives,
    mentions_minute,
    scorer_phrase_failures,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info
from tests.test_polish_wording import _first, _scripted, _settings

_TACTICAL_KINDS = (
    "signal_red_card",
    "signal_red_card_formation",
    "signal_formation_change",
    "possession_swing",
    "high_turnover_surge",
    "xg_surge",
)


def _goal(match_id: str, minute: int):  # type: ignore[no-untyped-def]
    events = generate_match(match_id)
    return events, next(e for e in events if e.type == "goal" and e.minute == minute)


# Scoring-team phrases


def test_scoring_phrases_must_belong_to_the_scoring_team() -> None:
    m02 = get_match_info("m02")
    wrong = (
        "Merehaven Athletic have their equaliser, and Port Calder FC are back on terms. "
        "The score is level at 1-1."
    )
    assert scorer_phrase_failures(wrong, m02, "away") == [
        "scorer phrase 'back on terms' is attached to Port Calder FC"
    ]
    assert not scorer_phrase_failures(
        "Merehaven Athletic level it through Nilo Rillstone. The score is level at 1-1.", m02, "away"
    )
    assert scorer_phrase_failures(
        "Port Calder FC have the opener. Port Calder lead 1-0.", m02, "away"
    )


def test_a_goal_line_with_the_scoring_phrase_on_the_other_team_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m02")
    events, equaliser = _goal("m02", 54)
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: (
            "Merehaven Athletic have their equaliser, and Port Calder FC are back on terms. "
            f"{p['score_phrase']}."
        ),
    )
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [equaliser], as_of=equaliser.seq)
    )
    assert len(prompts) == 1
    assert result.line is not None and result.line.source == "fallback"
    assert result.wording_failures == (
        "scorer phrase 'back on terms' is attached to Port Calder FC",
    )


# Consolation


def test_consolation_is_never_said_live() -> None:
    assert "consolation" not in COMMENTATOR_INSTRUCTIONS.casefold()
    assert "live only: consolation" in banned_phrase_hits(
        "A consolation for Ashbridge Rovers.", get_match_info("m01"), []
    )


def test_the_commentator_sees_a_goal_back_not_consolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _goal("m01", 74)
    pipeline, prompts = _scripted(
        monkeypatch, lambda p: f"Kiva Larkspur pulls a goal back for Ashbridge Rovers. {p['score_phrase']}."
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    values = [fact["value"] for fact in prompts[0]["facts"]]  # type: ignore[union-attr]
    assert "a goal back" in values and "consolation" not in values
    assert result.line is not None and result.line.source == "agent"


# Possessives and punctuation


def test_a_name_ending_in_s_takes_an_apostrophe_only() -> None:
    assert fix_possessives("Ashbridge Rovers's late goal.") == "Ashbridge Rovers' late goal."
    assert fix_possessives("Ashbridge Rovers’s press.") == "Ashbridge Rovers’ press."
    assert fix_possessives("Brio Fallowmere's finish.") == "Brio Fallowmere's finish."


def test_model_lines_get_the_possessive_and_a_final_full_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _goal("m01", 74)
    pipeline, _ = _scripted(
        monkeypatch,
        lambda p: f"Ashbridge Rovers's goal from a big chance. {p['score_phrase']}",
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert result.line is not None and result.line.source == "agent"
    assert result.line.text == "Ashbridge Rovers' goal from a big chance. Port Calder FC lead 2-1."


def test_every_line_ends_with_punctuation() -> None:
    assert end_with_punctuation("The score is level at 0-0") == "The score is level at 0-0."
    assert end_with_punctuation("Port Calder lead 1-0.") == "Port Calder lead 1-0."
    for kind, templates in FALLBACK_VARIANTS.items():
        for template in templates:
            assert template.rstrip()[-1] in ".!?", (kind, template)


# Phase words


@pytest.mark.parametrize(
    ("text", "minute", "period", "expected"),
    [
        ("Port Calder FC press early.", 10, 1, "Port Calder FC press early."),
        ("Port Calder FC press early on, and it works.", 27, 1, "Port Calder FC press and it works."),
        ("Port Calder FC tidy up before the break.", 39, 1, "Port Calder FC tidy up before the break."),
        ("Port Calder FC tidy up before the break.", 30, 1, "Port Calder FC tidy up."),
        ("Merehaven Athletic level it late in the half, and Port Calder FC sag.", 54, 2,
         "Merehaven Athletic level it and Port Calder FC sag."),
        ("Merehaven Athletic push early in the second half.", 50, 2,
         "Merehaven Athletic push early in the second half."),
        ("Merehaven Athletic push early in the second half.", 60, 2, "Merehaven Athletic push."),
        ("Late on, Ashbridge Rovers move to a 4-2-3-1.", 67, 2, "Ashbridge Rovers move to a 4-2-3-1."),
        ("Ivo Juniperby levels it, late on, from a half-chance.", 81, 2,
         "Ivo Juniperby levels it, late on, from a half-chance."),
        ("Ivo Juniperby levels it, late in the game, from a half-chance.", 74, 2,
         "Ivo Juniperby levels it from a half-chance."),
    ],
)
def test_phase_words_that_do_not_fit_the_minute_are_removed(
    text: str, minute: int, period: int, expected: str
) -> None:
    assert apply_phase_rules(text, minute=minute, period=period) == (expected, [])


@pytest.mark.parametrize(
    ("text", "minute"), [("Port Calder FC have an early goal.", 27), ("It is late on.", 60)]
)
def test_a_phase_word_that_cannot_be_removed_cleanly_fails(text: str, minute: int) -> None:
    _, failures = apply_phase_rules(text, minute=minute, period=1 if minute < 46 else 2)
    assert failures and "don't fit" in failures[0]


def test_m01_67_formation_line_loses_late_on(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, change = _first("m01", "formation_change", formation="4-2-3-1")
    pipeline, _ = _scripted(
        monkeypatch,
        lambda p: "Late on, Ashbridge Rovers move from a 4-3-3 to a 4-2-3-1, adding more in midfield.",
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [change], as_of=change.seq))
    assert result.line is not None and result.line.source == "agent"
    assert result.line.text.startswith("Ashbridge Rovers move from")
    assert "late on" not in result.line.text.casefold()


def test_a_broken_phase_removal_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m02")
    events, goal = _goal("m02", 27)
    pipeline, _ = _scripted(
        monkeypatch, lambda p: f"Port Calder FC have an early goal. {p['score_phrase']}."
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert result.line is not None and result.line.source == "fallback"
    assert any("don't fit" in failure for failure in result.wording_failures)


# Fallback variety


def test_every_tactical_fallback_has_at_least_three_versions() -> None:
    for kind in _TACTICAL_KINDS:
        assert len(FALLBACK_VARIANTS[kind]) >= 3, kind


def test_used_versions_come_last() -> None:
    values = {"team": "Ashbridge Rovers", "chance_descriptor": "the better of the chances",
              "window": 10, "score_phrase": "Port Calder lead 2-0"}
    used: set[str] = set()
    chosen = []
    for index in range(len(FALLBACK_VARIANTS["xg_surge"])):
        template, _ = next(fallback_choices("xg_surge", values, line_id=f"m01-l{index}", used=used))
        chosen.append(template)
        used.add(template)
    assert len(set(chosen)) == len(chosen)


def test_no_tactical_fallback_version_repeats_within_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Failing:
        async def run(self, prompt: str) -> SimpleNamespace:
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Failing())
    tracker = SignalTracker(info)
    used: set[str] = set()
    texts: list[str] = []
    # The dispatcher's cooldowns allow a handful of xG-surge lines a match (m01's live gate
    # had three); five windows, each with its own version.
    for event in events[::40]:
        if len(texts) == 5:
            break
        for signal in tracker.evaluate(events, as_of=event.seq):
            if signal.kind != "xg_surge" or len(texts) == 5:
                continue
            result = asyncio.run(
                pipeline.comment_on_window(
                    info, events, [event], as_of=event.seq, signals=[signal], used_fallbacks=used
                )
            )
            assert result.line is not None and result.line.source == "fallback"
            texts.append(result.line.text)
    assert len(texts) == 5
    assert len(used) == 5, texts


def test_fallback_says_the_better_of_the_chances() -> None:
    for template in FALLBACK_VARIANTS["xg_surge"]:
        assert "expected goals" not in template


# No empty restating


def test_non_goal_lines_never_restate_an_unchanged_nil_nil() -> None:
    nil_nil = "The score is level at 0-0"
    assert not score_phrase_allowed(nil_nil, [], goal_or_red_card=False)
    assert score_phrase_allowed("The score is level at 1-1", [], goal_or_red_card=False)
    assert not score_phrase_allowed(
        "Port Calder lead 1-0", ["a.", "Port Calder lead 1-0."], goal_or_red_card=False
    )


def test_a_tactical_line_at_nil_nil_gets_no_score(monkeypatch: pytest.MonkeyPatch) -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    first_goal = next(e for e in events if e.type == "goal")
    tracker = SignalTracker(info)
    window = next(
        (event, signal)
        for event in events[::40]
        if event.seq < first_goal.seq
        for signal in tracker.evaluate(events, as_of=event.seq)
        if signal.kind in {"possession_swing", "xg_surge", "high_turnover_surge"}
    )
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Failing:
        async def run(self, prompt: str) -> SimpleNamespace:
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Failing())
    event, signal = window
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [event], as_of=event.seq, signals=[signal])
    )
    assert result.line is not None and "0-0" not in result.line.text


# Comparisons


def test_a_comparison_must_name_its_period_and_point_swings_are_banned() -> None:
    assert comparison_failures("Port Calder FC have 63% possession, up from 45%.")
    assert not comparison_failures(
        "Port Calder FC have 63% possession, up from 45% in the previous 10 minutes."
    )
    assert "comparison: N-point swing" in banned_phrase_hits(
        "Port Calder FC have 63% possession and an 18-point swing.", get_match_info("m02"), []
    )
    for template in FALLBACK_VARIANTS["possession_swing"]:
        text = template.format(team="Port Calder FC", before=45, after=63, change=18, window=10,
                               window_words="ten", score_phrase="Port Calder lead 1-0")
        assert not comparison_failures(text), text
        assert not re.search(r"points?\s+swing", text)


# Late-drama clock


def test_a_late_peak_goal_may_give_its_minute_within_the_cap() -> None:
    lines = ["a.", "b.", "c.", "d.", "e.", "f.", "Late, with 9 minutes left.", "g."]
    assert not clock_budget_allows(lines)
    assert clock_budget_allows(lines, late_drama=True)
    capped = ["At minute 10, a.", "b.", "c.", "At minute 50, d."]
    assert not clock_budget_allows(capped, late_drama=True)


def test_the_88th_minute_winner_keeps_its_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, winner = _goal("m01", 88)
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: f"Juno Kestrelane puts Ashbridge Rovers in front in the 88th minute. {p['score_phrase']}.",
    )
    result = asyncio.run(
        pipeline.comment_on_window(
            info, events, [winner], as_of=winner.seq, clock_allowed=False, late_clock_allowed=True
        )
    )
    assert prompts[0]["clock_allowed"] is True
    assert result.line is not None and result.line.source == "agent"
    assert mentions_minute(result.line.text) and "88th minute" in result.line.text


# Cached lines and docs


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_cached_lines_are_published_with_final_punctuation(match_id: str) -> None:
    info, events = get_match_info(match_id), generate_match(match_id)
    by_id = {event.event_id: event for event in events}
    pipeline = PunditPipeline(replace(_settings(), mode="cached"), None)
    cached = load_cached_match(match_id, cache_dir=Path("data/cached"))
    published = 0
    for line in cached.commentary:
        event = max((by_id[event_id] for event_id in line.event_ids), key=lambda e: e.seq)
        result = asyncio.run(pipeline.comment_on_window(info, events, [event], as_of=event.seq))
        assert result.line is not None, result.notice
        assert result.line.text[-1] in ".!?", result.line.text
        assert "consolation" not in result.line.text.casefold()
        published += 1
    assert published == len(cached.commentary)


def test_build_prompt_has_no_stale_replay_lines() -> None:
    text = Path("prompts/build-prompt-v2.md").read_text(encoding="utf-8")
    assert "At most 3 replays" not in text
    assert "There is no resume" not in text
    assert "If 3 replays are already running, return 503" not in text
    assert "At most 6 replays run at once, and at most 3 from one IP address" in text


# Window length, withheld score, possession fallback, "a player short"


def _plain_line(text: str, event_id: str):  # type: ignore[no-untyped-def]
    from second_screen_pundit.agents.models import CommentaryLine

    return CommentaryLine(
        line_id="x", text=text, insight_id="i", tone="measured", event_ids=[event_id]
    )


@pytest.mark.parametrize(
    "text",
    [
        "Port Calder FC have pressed hard over the last 10 minutes.",
        "Port Calder FC have pressed hard over the last ten minutes.",
        "Port Calder FC have been on top for the past 10 minutes.",
        "Port Calder FC sat deeper in the previous 5 minutes.",
    ],
)
def test_the_configured_window_length_is_grounded(text: str) -> None:
    from second_screen_pundit.agents.evidence import check_evidence

    events, info = generate_match("m02"), get_match_info("m02")
    report = check_evidence(
        _plain_line(text, events[10].event_id), events=events, as_of=events[-1].seq,
        tool_results=[], info=info, input_event_ids=[events[10].event_id],
    )
    assert report.passed, report.failures


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Port Calder FC have pressed hard over the last 12 minutes.", "12"),
        ("Port Calder FC have pressed hard over the last seven minutes.", "seven"),
        ("Port Calder FC will press for 10 minutes.", "10"),
    ],
)
def test_any_other_minute_count_still_fails(text: str, number: str) -> None:
    from second_screen_pundit.agents.evidence import check_evidence

    events, info = generate_match("m02"), get_match_info("m02")
    report = check_evidence(
        _plain_line(text, events[10].event_id), events=events, as_of=events[-1].seq,
        tool_results=[], info=info, input_event_ids=[events[10].event_id],
    )
    assert f"ungrounded number {number!r}" in report.failures


def test_a_withheld_score_sentence_is_removed_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from second_screen_pundit.agents.wording import strip_withheld_score

    assert strip_withheld_score("Port Calder FC press. The score is level at 0-0") == (
        "Port Calder FC press."
    )
    assert strip_withheld_score("The score is level at 0-0.") == "The score is level at 0-0."
    info = get_match_info("m01")
    events, substitution = _first("m01", "substitution")
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: "Loran Mallowfen comes on for Ashbridge Rovers. Port Calder lead 2-0.",
    )
    result = asyncio.run(
        pipeline.comment_on_window(
            info, events, [substitution], as_of=substitution.seq,
            recent_lines=("Port Calder FC push on. Port Calder lead 2-0.",),
        )
    )
    assert prompts[0]["score_phrase"] is None and len(prompts) == 1
    assert result.line is not None and result.line.source == "agent"
    assert result.line.text == "Loran Mallowfen comes on for Ashbridge Rovers."


def test_the_possession_fallback_names_both_periods() -> None:
    values = {"team": "Port Calder FC", "before": 45, "after": 63, "window": 10,
              "window_words": "ten"}
    texts = list(fallback_lines_for("possession_swing", values))
    assert (
        "Port Calder FC have taken over the ball: 63% of the possession in the last ten "
        "minutes, up from 45% before that." in texts
    )
    assert not any("points" in text for text in texts)


def fallback_lines_for(kind: str, values: dict) -> list[str]:
    from second_screen_pundit.agents.fallbacks import fallback_lines

    return list(fallback_lines(kind, values, line_id="m02-l0498-tactical"))


def test_a_man_short_becomes_a_player_short(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m02")
    events, red = _first("m02", "card", colour="red")
    pipeline, _ = _scripted(
        monkeypatch,
        lambda p: (
            "Quen Rillstone sees red, and Port Calder FC must see this out a man short. "
            f"{p['score_phrase']}."
        ),
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [red], as_of=red.seq))
    assert result.line is not None and result.line.source == "agent"
    assert "a player short" in result.line.text and "a man" not in result.line.text


def test_the_comparison_example_has_no_window_number() -> None:
    assert "up from 45% before that" in COMMENTATOR_INSTRUCTIONS
    assert "previous 10 minutes" not in COMMENTATOR_INSTRUCTIONS


# Score citation, match averages and red-card wording


def _uncited(monkeypatch: pytest.MonkeyPatch, reply):  # type: ignore[no-untyped-def]
    """A commentator that cites only the insight's events, never its facts."""
    from second_screen_pundit.agents.models import CommentaryReply

    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    prompts: list[dict] = []

    class Agent:
        async def run(self, prompt: str) -> SimpleNamespace:
            import json

            payload = json.loads(prompt)
            prompts.append(payload)
            return SimpleNamespace(
                value=CommentaryReply(
                    text=reply(payload), fact_ids=[],
                    event_ids=payload["insight"]["event_ids"],  # type: ignore[index]
                )
            )

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Agent())
    return pipeline, prompts


def test_a_line_stating_the_supplied_score_cites_the_score_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m02")
    events, winner = _goal("m02", 82)  # Merehaven, away, lead 2-1: the away-leader rule applies
    pipeline, prompts = _uncited(
        monkeypatch,
        lambda p: f"Orin Silvergrove puts Merehaven Athletic in front. {p['score_phrase']}.",
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [winner], as_of=winner.seq))
    assert len(prompts) == 1
    assert result.line is not None and result.line.source == "agent", result.evidence
    assert any("score" in fact_id for fact_id in result.line.fact_ids)


def test_a_wrong_score_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m02")
    events, winner = _goal("m02", 82)
    pipeline, prompts = _uncited(
        monkeypatch, lambda p: "Orin Silvergrove puts Merehaven Athletic in front. Merehaven lead 3-1."
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [winner], as_of=winner.seq))
    assert len(prompts) == 2
    assert result.line is not None and result.line.source == "fallback"
    assert "3-1" not in result.line.text


@pytest.mark.parametrize(
    ("recent", "average", "expected"),
    [(3, 2.5, None), (3, 2.0, "above their usual rate"), (3, 1.5, "well above their usual rate"),
     (2, 0.0, "well above their usual rate"), (0, 0.0, None)],
)
def test_rate_description_thresholds(recent: int, average: float, expected: str | None) -> None:
    from second_screen_pundit.agents.signals import rate_description

    assert rate_description(recent, average) == expected


def test_the_commentator_never_sees_a_raw_match_average(monkeypatch: pytest.MonkeyPatch) -> None:
    from second_screen_pundit.agents.models import (
        CommentaryReply,
        DirectorDecision,
        Insight,
    )
    from second_screen_pundit.agents.signals import MATCH_AVERAGE, RATE_DESCRIPTION

    info, events = get_match_info("m01"), generate_match("m01")
    tracker = SignalTracker(info)
    event, signal = next(
        (event, signal)
        for event in events[::40]
        for signal in tracker.evaluate(events, as_of=event.seq)
        if signal.kind == "high_turnover_surge"
    )
    average = next(f for f in signal.facts if f.fact_id.endswith(MATCH_AVERAGE))
    description = next(f for f in signal.facts if f.fact_id.endswith(RATE_DESCRIPTION))
    recent = next(f for f in signal.facts if f.fact_id.endswith("_recent"))
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    commentator_prompts: list[dict] = []

    def agent(role, _instructions, **_kwargs):  # type: ignore[no-untyped-def]
        class Agent:
            async def run(self, prompt: str) -> SimpleNamespace:
                import json

                payload = json.loads(prompt)
                if role == "director":
                    value: object = DirectorDecision(
                        should_comment=True, importance=1, reason="Pressing surge",
                        fact_ids=[], event_ids=signal.event_ids[:1],
                    )
                elif role == "analyst":
                    value = Insight(
                        insight_id="i1", minute=event.minute, headline="Ashbridge press high",
                        importance=1, fact_ids=[recent.fact_id, average.fact_id],
                        event_ids=recent.event_ids[:1],
                    )
                else:
                    commentator_prompts.append(payload)
                    value = CommentaryReply(
                        text=(
                            f"{team} win the ball high, {description.value}."
                            if (team := info.home.name if signal.team == "home" else info.away.name)
                            else ""
                        ),
                        fact_ids=[description.fact_id],
                        event_ids=payload["insight"]["event_ids"],
                    )
                return SimpleNamespace(value=value)

        return Agent()

    monkeypatch.setattr(pipeline, "_agent", agent)
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [event], as_of=event.seq, signals=[signal])
    )
    prompt = commentator_prompts[0]
    shown = [fact["fact_id"] for fact in prompt["facts"]]
    assert average.fact_id not in shown and average.fact_id not in prompt["insight"]["fact_ids"]
    assert description.fact_id in shown
    assert str(average.value) not in str(prompt["facts"])
    assert result.line is not None and result.line.source == "agent", result.evidence
    assert "usual rate" in result.line.text


def test_red_card_lines_describe_the_consequence() -> None:
    assert "RED CARDS. Describe the consequence" in COMMENTATOR_INSTRUCTIONS
    assert "a player short" in COMMENTATOR_INSTRUCTIONS
