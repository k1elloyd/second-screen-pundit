from second_screen_pundit.agents.gate import _WindowMeasurement, _window_summary
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import AgentCallTiming, LiveWindowResult


def _commentary_result(duration_ms: int) -> LiveWindowResult:
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
        timings=(AgentCallTiming("commentator", 1, duration_ms),),
    )


def test_window_summary_reports_commentator_median_slowest_and_drop_reason() -> None:
    measurements = [
        _WindowMeasurement("fast_path", result=_commentary_result(140)),
        _WindowMeasurement("fast_path", result=_commentary_result(260)),
        _WindowMeasurement(
            "fast_path",
            result=LiveWindowResult(
                status="timed_out",
                line=None,
                insight=None,
                tool_results=(),
                evidence=None,
                notice="Live commentary was dropped because its time budget expired.",
            ),
        ),
    ]

    summary = _window_summary(measurements, "fast_path")

    assert summary["windows"] == 3
    assert summary["lines"] == 2
    assert summary["agent_calls"] == [
        {"role": "commentator", "attempt": 1, "duration_ms": 140},
        {"role": "commentator", "attempt": 1, "duration_ms": 260},
    ]
    assert summary["commentator_ms_per_line"] == {
        "count": 2,
        "median": 200.0,
        "slowest": 260,
    }
    assert summary["dropped_lines"] == 1
    assert summary["drop_reasons"] == {
        "Live commentary was dropped because its time budget expired.": 1
    }
