from second_screen_pundit.agents.dispatcher import WindowOutcome
from second_screen_pundit.agents.latency import summarize_calls, summarize_path
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import AgentCallTiming, LiveWindowResult
from second_screen_pundit.engine.generate import generate_match


def _outcome(
    path: str,
    result: LiveWindowResult,
    *,
    elapsed_ms: int = 100,
) -> WindowOutcome:
    event = generate_match("m01")[0]
    return WindowOutcome(path, event, 1, result=result, elapsed_ms=elapsed_ms)  # type: ignore[arg-type]


def _commentary(duration_ms: int, reasoning: int | None = 40) -> LiveWindowResult:
    return LiveWindowResult(
        status="commentary",
        line=CommentaryLine(
            line_id="m01-l001",
            text="A measured line.",
            insight_id="m01-i0001",
            tone="measured",
        ),
        insight=None,
        tool_results=(),
        evidence=None,
        timings=(
            AgentCallTiming("commentator", 1, duration_ms, 500, 120, reasoning),
        ),
    )


def test_path_summary_reports_commentator_median_slowest_and_drop_reasons() -> None:
    outcomes = [
        _outcome("fast_path", _commentary(140), elapsed_ms=150),
        _outcome("fast_path", _commentary(260), elapsed_ms=300),
        _outcome(
            "fast_path",
            LiveWindowResult("timed_out", None, None, (), None, "expired"),
        ),
        _outcome(
            "tactical",
            LiveWindowResult("skipped", None, None, (), None, None),
        ),
        _outcome(
            "tactical",
            LiveWindowResult(
                "skipped",
                None,
                None,
                (),
                None,
                "The tactical window became stale before commentary completed.",
            ),
        ),
    ]

    fast = summarize_path(outcomes, "fast_path", 15)
    tactical = summarize_path(outcomes, "tactical", 15)

    assert fast["windows"] == 3 and fast["lines"] == 2
    assert fast["commentator_ms"] == {"count": 2, "median": 200.0, "slowest": 260}
    assert fast["run_ms"] == {"count": 2, "median": 225.0, "slowest": 300}
    assert fast["drop_reasons"] == {"time budget of 15 s expired": 1}
    assert tactical["lines"] == 0
    assert tactical["drop_reasons"] == {
        "Director declined to comment": 1,
        "stale: a newer tactical window was dispatched": 1,
    }


def test_call_summary_reports_token_breakdown_per_role() -> None:
    outcomes = [
        _outcome("fast_path", _commentary(100, reasoning=40)),
        _outcome("fast_path", _commentary(300, reasoning=80)),
    ]

    summary = summarize_calls(outcomes)

    assert summary["commentator"]["calls"] == 2
    assert summary["commentator"]["median_reasoning_tokens"] == 60.0
    assert summary["commentator"]["median_output_tokens"] == 120
    assert summary["commentator"]["median_input_tokens"] == 500
