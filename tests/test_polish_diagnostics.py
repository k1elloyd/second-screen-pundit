"""Polish session, group 4: why checks fail, and token counts."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents.clients import UsageCapture, capture_usage
from second_screen_pundit.agents.dispatcher import WindowOutcome
from second_screen_pundit.agents.evidence import classify_failure
from second_screen_pundit.agents.latency import (
    summarize_failures,
    summarize_tokens,
    wording_checks,
)
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import AgentCallTiming, LiveWindowResult, _call_usage
from second_screen_pundit.engine.generate import generate_match, get_match_info
from tests.test_polish_wording import _first, _scripted


@pytest.mark.parametrize(
    ("failure", "rule", "subject"),
    [
        ("ungrounded number '57'", "number not grounded by a cited fact for the named team and metric", "57"),
        ("formation '3-4-3' does not match a cited formation fact",
         "formation does not match a cited formation for the named team", "3-4-3"),
        ("score '2-1' does not match a cited score fact", "score does not match the score fact", "2-1"),
        ("percentage '53' must be spoken with % or per cent", "percentage without % or per cent", "53"),
        ("'a back four': describe the back line only when the first number of the formation "
         "changes; otherwise say what changed in midfield or attack, or leave it out",
         "back line mentioned without a change in its number", "a back four"),
        ("banned phrase hype: incredible", "banned phrase", "hype: incredible"),
        ("something new", "other", None),
    ],
)
def test_failures_are_classified_by_rule_and_subject(failure: str, rule: str, subject: str | None) -> None:
    reason = classify_failure(failure)
    assert (reason.rule, reason.subject) == (rule, subject)


def test_live_window_records_and_logs_why_the_line_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, _ = _scripted(
        monkeypatch, lambda p: f"Port Calder FC have 57 shots now. {p['score_phrase']}."
    )
    with caplog.at_level(logging.WARNING, logger="second_screen_pundit"):
        result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert result.line is not None and result.line.source == "fallback"
    rule = "number not grounded by a cited fact for the named team and metric"
    assert result.evidence_failures == (f"commentator: {rule}: 57",) * 2
    logged = [r.getMessage() for r in caplog.records if "evidence_check_failed" in r.getMessage()]
    assert len(logged) == 2 and "subject='57'" in logged[0] and "role=commentator" in logged[0]


def test_usage_is_read_from_the_chat_responses() -> None:
    async def run() -> dict[str, int]:
        middleware = UsageCapture()
        with capture_usage() as captured:
            for usage in (
                {"input_token_count": 1200, "output_token_count": 90},
                {"input_token_count": 300, "output_token_count": 10,
                 "reasoning_output_token_count": 64},
            ):
                context = SimpleNamespace(result=SimpleNamespace(usage_details=usage))

                async def call_next() -> None:
                    return None

                await middleware.process(context, call_next)  # type: ignore[arg-type]
        return captured

    captured = asyncio.run(run())
    assert captured == {
        "input_token_count": 1500,
        "output_token_count": 100,
        "reasoning_output_token_count": 64,
    }
    # The workflow's own response has usage_details None: the captured counts win.
    assert _call_usage(SimpleNamespace(usage_details=None), captured)["input_token_count"] == 1500
    assert _call_usage(SimpleNamespace(usage_details={"input_token_count": 7}), {}) == {
        "input_token_count": 7
    }


def test_usage_capture_outside_a_call_does_nothing() -> None:
    async def run() -> None:
        context = SimpleNamespace(result=SimpleNamespace(usage_details={"input_token_count": 5}))

        async def call_next() -> None:
            return None

        await UsageCapture().process(context, call_next)  # type: ignore[arg-type]

    asyncio.run(run())


def _outcome(text: str, *, minute: int = 10, event_ids: list[str] | None = None,
             failures: tuple[str, ...] = (), banned: tuple[str, ...] = (),
             timings: tuple[AgentCallTiming, ...] = ()) -> WindowOutcome:
    events = generate_match("m02")
    line = CommentaryLine(
        line_id=f"x-{minute}", text=text, insight_id="i", tone="measured",
        event_ids=event_ids or [events[0].event_id],
    )
    return WindowOutcome(
        "tactical", events[0], minute,
        result=LiveWindowResult(
            "commentary", line, None, (), None, timings=timings,
            evidence_failures=failures, banned_phrases=banned,
        ),
    )


def test_latency_report_summaries() -> None:
    info, events = get_match_info("m02"), generate_match("m02")
    red = next(e for e in events if e.type == "card" and e.detail.get("colour") == "red")
    timing = AgentCallTiming("commentator", 1, 900, input_tokens=1000, output_tokens=80)
    outcomes = [
        _outcome("Port Calder FC have 61% to 39% of the possession. Port Calder lead 1-0.",
                 failures=("commentator: number not grounded: 57",), timings=(timing,)),
        _outcome("Quen Rillstone is sent off. Port Calder FC lead 1-0, and Port Calder FC sit deep.",
                 event_ids=[red.event_id], banned=("hype: incredible",),
                 timings=(timing, AgentCallTiming("commentator", 2, 800))),
        _outcome("Merehaven Athletic push on at minute 80, with 10 minutes left."),
    ]
    failures = summarize_failures(outcomes)
    assert failures["by_reason"] == {"number not grounded": 1}
    tokens = summarize_tokens(outcomes)
    assert (tokens["input_tokens"], tokens["output_tokens"], tokens["calls_without_usage"]) == (
        2000, 160, 1,
    )
    checks = wording_checks(info, events, outcomes)
    assert checks["red_card_lines"] == 1
    assert len(checks["minute_and_minutes_left"]) == 1
    assert checks["clock_lines"] == 1
    assert len(checks["fc_after_first_mention"]) == 1
    assert checks["possession_pairs"][0]["sums_to_100"] is True
    assert checks["banned_phrase_rejections"]["rejected"] == 1
    assert checks["banned_phrase_rejections"]["commentator_calls"] == 3
