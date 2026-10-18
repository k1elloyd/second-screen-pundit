"""Comparisons in words are checked against possession, shots and xG.

At the viewer's 59th minute of m01: Ashbridge Rovers (home) have 43% of the possession and
19 shots to Port Calder's 20 for the match so far, and xG 2.037 to 2.044; over the last 15
minutes Ashbridge have the higher xG.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from second_screen_pundit.agents.comparisons import comparison_failures, find_comparisons
from second_screen_pundit.agents.models import ExplainerReply, RecapReply
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.profile import ViewerProfile
from tests.test_polish_wording import _first, _scripted
from tests.test_review2_fixes import _recap_following_the_prompt, _scripted_pipeline

INFO = get_match_info("m01")
EVENTS = generate_match("m01")
AS_OF_59 = max(event.seq for event in EVENTS if event.minute < 60)


def _check(text: str, my_team: str | None = None) -> list[str]:
    return comparison_failures(text, INFO, EVENTS, AS_OF_59, my_team=my_team)


def test_the_43_per_cent_case_fails() -> None:
    failures = _check("Your side have had more of the ball.", my_team="home")
    assert failures == [
        "comparison 'more of the ball' for Ashbridge Rovers is not true of possession for "
        "the match so far or the current spell; say it the other way round or leave it out"
    ]
    # Named, not framed, it is just as false.
    assert _check("Ashbridge Rovers have had more of the ball.")


def test_a_true_comparison_passes() -> None:
    assert _check("Port Calder FC have had more of the ball.") == []
    assert _check("Your side have had less of the ball.", my_team="home") == []
    assert _check("Ashbridge Rovers are under pressure, and Port Calder FC are on top.") == []


def test_a_comparison_true_for_the_spell_but_not_the_match_passes() -> None:
    # Match so far: xG 2.037 to 2.044, so Port Calder have had the better of the chances.
    # The last 15 minutes: Ashbridge's xG is higher.
    assert _check("Ashbridge Rovers have had the better of the chances.") == []
    # False for both: Ashbridge have had fewer shots for the match and the spells.
    assert _check("Ashbridge Rovers have had more shots.")


def test_the_phrase_belongs_to_the_nearest_team_before_it_and_not_reverses_it() -> None:
    found = find_comparisons(
        "Port Calder FC lead, but Ashbridge Rovers have had more of the ball.", INFO
    )
    assert [(c.phrase, c.team, c.leads) for c in found] == [("more of the ball", "home", True)]
    assert _check("Ashbridge Rovers have not had more of the ball.") == []
    # No team before the phrase: nothing to attach it to, so nothing is checked.
    assert _check("More of the ball would help.") == []
    # "Your side" means nothing without My team.
    assert _check("Your side have had more of the ball.") == []


def _comparison_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "comparison_failed" in record.getMessage()]


def test_a_false_comparison_in_a_line_is_retried_then_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="second_screen_pundit")
    events, goal = _first("m01", "goal")  # Port Calder's opener; Ashbridge have less of the ball
    pipeline, prompts = _scripted(
        monkeypatch,
        lambda p: f"Ashbridge Rovers have had more of the ball. {p['score_phrase']}.",
    )
    result = asyncio.run(pipeline.comment_on_window(INFO, events, [goal], as_of=goal.seq))
    assert len(prompts) == 2
    assert "comparison 'more of the ball'" in prompts[1]["retry_feedback"]
    assert result.line is not None and result.line.source == "fallback"
    assert len(_comparison_logs(caplog)) == 2


def test_a_false_comparison_in_an_answer_is_retried_with_the_phrase_then_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="second_screen_pundit")

    def wrong(_prompt: dict) -> ExplainerReply:
        return ExplainerReply(answer="Your side have had more of the ball.", fact_ids=[])

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [wrong, wrong])
    result = asyncio.run(
        pipeline.answer_question(
            INFO, EVENTS, "Who is on top?", as_of=AS_OF_59,
            profile=ViewerProfile("casual", "home"),
        )
    )
    assert "comparison 'more of the ball' for Ashbridge Rovers" in prompts[1]["retry_feedback"]
    assert result.source == "fallback"
    assert len(_comparison_logs(caplog)) == 2


def test_a_false_comparison_in_the_recap_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def wrong(prompt: dict) -> RecapReply:
        reply = _recap_following_the_prompt(prompt)
        # m01 at full time: Port Calder had more shots for the match and both spells.
        return reply.model_copy(
            update={"summary": reply.summary + " Ashbridge Rovers had more shots."}
        )

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [wrong, _recap_following_the_prompt])
    result = asyncio.run(pipeline.produce_recap(INFO, EVENTS, as_of=EVENTS[-1].seq))
    assert "comparison 'more shots'" in prompts[1]["retry_feedback"]
    assert result.source == "agent" and result.attempts == 2
