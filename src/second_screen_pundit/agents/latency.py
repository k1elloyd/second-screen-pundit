from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from statistics import median
from time import perf_counter

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.dispatcher import LiveDispatcher, WindowOutcome
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay


def _stats(values: list[int]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median": median(values) if values else None,
        "slowest": max(values) if values else None,
    }


def _commentator_ms(outcome: WindowOutcome) -> int:
    if outcome.result is None:
        return 0
    return sum(t.duration_ms for t in outcome.result.timings if t.role == "commentator")


def drop_reason(outcome: WindowOutcome, budget_seconds: float) -> str:
    if outcome.error:
        return f"error: {outcome.error}"
    result = outcome.result
    if result is None:
        return "not run"
    if result.status == "skipped":
        if result.notice and "stale" in result.notice:
            return "stale: a newer tactical window was dispatched"
        return "Director declined to comment"
    if result.status == "timed_out":
        return f"time budget of {budget_seconds:g} s expired"
    if result.status == "rejected":
        failures = "; ".join(result.evidence.failures) if result.evidence else "unknown"
        if result.notice and "Structured output" in result.notice:
            return result.notice
        return f"evidence check failed twice: {failures}"
    return result.status


def summarize_path(
    outcomes: list[WindowOutcome],
    path: str,
    budget_seconds: float,
) -> dict[str, object]:
    selected = [outcome for outcome in outcomes if outcome.path == path]
    lines = [
        outcome
        for outcome in selected
        if outcome.result is not None and outcome.result.status == "commentary"
    ]
    fallback_lines = [
        outcome
        for outcome in lines
        if outcome.result is not None
        and outcome.result.line is not None
        and outcome.result.line.source == "fallback"
    ]
    dropped = [outcome for outcome in selected if outcome not in lines]
    reasons = Counter(drop_reason(outcome, budget_seconds) for outcome in dropped)
    return {
        "windows": len(selected),
        "lines": len(lines),
        "fallback_lines": len(fallback_lines),
        "fallback_reasons": dict(
            Counter(
                outcome.result.notice or "unspecified"
                for outcome in fallback_lines
                if outcome.result is not None
            )
        ),
        "commentator_ms": _stats([_commentator_ms(outcome) for outcome in lines]),
        "run_ms": _stats([outcome.elapsed_ms or 0 for outcome in lines]),
        "dropped": len(dropped),
        "drop_reasons": dict(reasons),
    }


def summarize_calls(outcomes: list[WindowOutcome]) -> dict[str, dict[str, object]]:
    by_role: dict[str, list[object]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.result is not None:
            for timing in outcome.result.timings:
                by_role[timing.role].append(timing)

    def med(values: list[int | None]) -> float | None:
        present = [value for value in values if value is not None]
        return median(present) if present else None

    return {
        role: {
            "calls": len(timings),
            "duration_ms": _stats([t.duration_ms for t in timings]),  # type: ignore[attr-defined]
            "median_input_tokens": med([t.input_tokens for t in timings]),  # type: ignore[attr-defined]
            "median_output_tokens": med([t.output_tokens for t in timings]),  # type: ignore[attr-defined]
            "median_reasoning_tokens": med([t.reasoning_tokens for t in timings]),  # type: ignore[attr-defined]
        }
        for role, timings in sorted(by_role.items())
    }


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger = logging.getLogger("second_screen_pundit")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def run_live_latency_gate(match_id: str = "m01", speed: int = 20) -> dict[str, object]:
    load_dotenv()
    settings = AgentSettings.from_environment()
    if settings.mode != "live":
        raise RuntimeError("The live latency gate requires NARRATIVE_MODE=live")
    _configure_logging()
    info = get_match_info(match_id)
    events = generate_match(match_id)

    with DefaultAzureCredential() as credential:
        pipeline = PunditPipeline(settings, credential)
        warm_up = await pipeline.warm_up()
        print("Foundry warm-up: " + json.dumps(warm_up), flush=True)

        delay = BroadcastDelay(settings.broadcast_delay_seconds)
        replay = Replay(events, speed)
        viewer = delay.events(replay.subscribe())
        viewer_count = 0

        async def consume_viewer_stream() -> None:
            nonlocal viewer_count
            async for _ in viewer:
                viewer_count += 1

        dispatcher = LiveDispatcher(pipeline, info, events, settings, delay=delay)
        viewer_task = asyncio.create_task(consume_viewer_stream())
        replay.start()
        started = perf_counter()
        try:
            await dispatcher.run(
                replay.stream(),
                replay_stopped=lambda: replay.state == "stopped",
            )
            replay_ms = round((perf_counter() - started) * 1000)
            await viewer_task
        finally:
            await replay.stop()
            viewer_task.cancel()
            await asyncio.gather(viewer_task, return_exceptions=True)

    budget = settings.live_timeout_seconds
    outcomes = dispatcher.outcomes
    tactical = summarize_path(outcomes, "tactical", budget)
    tactical["boundaries"] = {
        "total": len(dispatcher.boundaries),
        **Counter(record.decision for record in dispatcher.boundaries),
    }
    tactical["lines_produced"] = [
        {
            "minute": outcome.minute,
            "signals": [signal.kind for signal in outcome.signals],
            "text": outcome.result.line.text,  # type: ignore[union-attr]
            "source": outcome.result.line.source,  # type: ignore[union-attr]
            "run_ms": outcome.elapsed_ms,
            "calls_ms": {
                f"{timing.role}#{timing.attempt}": timing.duration_ms
                for timing in outcome.result.timings  # type: ignore[union-attr]
            },
        }
        for outcome in sorted(outcomes, key=lambda item: item.event.seq)
        if outcome.path == "tactical"
        and outcome.result is not None
        and outcome.result.status == "commentary"
    ]
    lines = [
        outcome
        for outcome in outcomes
        if outcome.result is not None and outcome.result.status == "commentary"
    ]
    observed = [
        delay.released[seq] - delay.seen_at(seq) for seq in delay.released
    ]
    late = [outcome.released_late_ms or 0 for outcome in lines if outcome.released_late_ms]
    report: dict[str, object] = {
        "match_id": match_id,
        "speed": speed,
        "budget_ms": round(budget * 1000),
        "commentator_reasoning_effort": settings.commentator_reasoning_effort,
        "replay_elapsed_ms": replay_ms,
        "fast_path": summarize_path(outcomes, "fast_path", budget),
        "tactical": tactical,
        "fallback_lines": [
            {
                "path": outcome.path,
                "minute": outcome.minute,
                "line_id": outcome.result.line.line_id,  # type: ignore[union-attr]
                "text": outcome.result.line.text,  # type: ignore[union-attr]
                "reason": outcome.result.notice,
            }
            for outcome in sorted(outcomes, key=lambda item: item.event.seq)
            if outcome.result is not None
            and outcome.result.status == "commentary"
            and outcome.result.line is not None
            and outcome.result.line.source == "fallback"
        ],
        "agent_calls_by_role": summarize_calls(outcomes),
        "viewer_stream": {
            "configured_delay_s": settings.broadcast_delay_seconds,
            "events_released": viewer_count,
            "observed_delay_s": {
                "min": round(min(observed), 3) if observed else None,
                "max": round(max(observed), 3) if observed else None,
            },
            "lines_released": len(lines),
            "lines_ready_before_event_release": len(lines) - len(late),
            "lines_ready_after_event_release": {
                "count": len(late),
                "max_late_ms": max(late) if late else 0,
            },
        },
    }
    print("Full-match latency gate: " + json.dumps(report), flush=True)
    return report
