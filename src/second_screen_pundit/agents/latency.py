from __future__ import annotations

import asyncio
import json
import logging
import re
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
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name
from second_screen_pundit.agents.wording import (
    mentions_clock,
    mentions_minute,
    mentions_minutes_left,
)


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
            "finish_reasons": dict(
                Counter(str(t.finish_reason) for t in timings)  # type: ignore[attr-defined]
            ),
        }
        for role, timings in sorted(by_role.items())
    }


def summarize_failures(outcomes: list[WindowOutcome]) -> dict[str, object]:
    """Every failed check in the run, counted by the rule it broke, with what broke it."""
    entries = [
        entry
        for outcome in outcomes
        if outcome.result is not None
        for entry in outcome.result.evidence_failures
    ]
    by_reason: Counter[str] = Counter()
    subjects: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in entries:
        role, rule, subject = (entry.split(": ", 2) + ["-", "-"])[:3]
        by_reason[rule] += 1
        subjects[rule][f"{role}: {subject}"] += 1
    return {
        "total": len(entries),
        "by_reason": dict(by_reason.most_common()),
        "subjects": {rule: dict(counter.most_common()) for rule, counter in subjects.items()},
    }


def summarize_tokens(outcomes: list[WindowOutcome]) -> dict[str, object]:
    turns = [
        {
            "path": outcome.path,
            "minute": outcome.minute,
            "calls": len(outcome.result.timings),
            "input_tokens": sum(t.input_tokens or 0 for t in outcome.result.timings),
            "output_tokens": sum(t.output_tokens or 0 for t in outcome.result.timings),
            "missing_usage": sum(t.input_tokens is None for t in outcome.result.timings),
        }
        for outcome in sorted(outcomes, key=lambda item: item.event.seq)
        if outcome.result is not None and outcome.result.timings
    ]
    return {
        "input_tokens": sum(turn["input_tokens"] for turn in turns),  # type: ignore[misc]
        "output_tokens": sum(turn["output_tokens"] for turn in turns),  # type: ignore[misc]
        "calls_without_usage": sum(turn["missing_usage"] for turn in turns),  # type: ignore[misc]
        "turns": turns,
    }


_POSSESSION_PAIR = re.compile(r"(\d+(?:\.\d+)?)\s*%[^.%]{0,40}?\b(?:to|and|against|,)\s+(\d+(?:\.\d+)?)\s*%")


def wording_checks(
    info: MatchInfo, events: list[Event], outcomes: list[WindowOutcome]
) -> dict[str, object]:
    """The polish-session gate checks on the published lines."""
    lines = [
        outcome.result.line
        for outcome in sorted(outcomes, key=lambda item: item.event.seq)
        if outcome.result is not None
        and outcome.result.status in {"commentary", "cached"}
        and outcome.result.line is not None
    ]
    texts = [line.text for line in lines]
    clock = [text for text in texts if mentions_clock(text)]
    both = [text for text in texts if mentions_minute(text) and mentions_minutes_left(text)]
    late_fc = []
    for text in texts:
        for side in ("home", "away"):
            name = team_name(info, side)
            mentions = re.findall(
                rf"(?<!\w)(?:{re.escape(name.full)}|{re.escape(name.short)})(?!\w)", text
            )
            if " FC" in name.full and name.full in mentions[1:]:
                late_fc.append(text)
    red_ids = {
        event.event_id
        for event in events
        if event.type == "card" and event.detail.get("colour") == "red"
    }
    pairs = [
        (text, float(a), float(b))
        for text in texts
        for a, b in _POSSESSION_PAIR.findall(text)
    ]
    commentator_calls = sum(
        1
        for outcome in outcomes
        if outcome.result is not None
        for timing in outcome.result.timings
        if timing.role == "commentator"
    )
    banned = [
        {"minute": outcome.minute, "phrases": list(outcome.result.banned_phrases)}
        for outcome in outcomes
        if outcome.result is not None and outcome.result.banned_phrases
    ]
    wording = [
        {"minute": outcome.minute, "failures": list(outcome.result.wording_failures)}
        for outcome in outcomes
        if outcome.result is not None and outcome.result.wording_failures
    ]
    fallback_texts = [line.text for line in lines if line.source == "fallback"]
    tactical_nil_nil = [
        outcome.result.line.text
        for outcome in outcomes
        if outcome.path == "tactical"
        and outcome.result is not None
        and outcome.result.line is not None
        and "level at 0-0" in outcome.result.line.text
    ]
    return {
        "lines": len(texts),
        "no_end_punctuation": [text for text in texts if text.rstrip()[-1:] not in ".!?"],
        "s_apostrophe_s": [text for text in texts if re.search(r"\b[A-Z][\w-]*s['’]s\b", text)],
        "consolation": [text for text in texts if "consolation" in text.casefold()],
        "point_swing": [text for text in texts if re.search(r"\d+[- ]points?\s+swing", text)],
        "tactical_nil_nil": tactical_nil_nil,
        "repeated_fallback_lines": sorted(
            {text for text in fallback_texts if fallback_texts.count(text) > 1}
        ),
        "wording_rejections": {"rejected": len(wording), "details": wording},
        "minute_and_minutes_left": both,
        "clock_lines": len(clock),
        "clock_share": round(len(clock) / len(texts), 3) if texts else 0.0,
        "fc_after_first_mention": late_fc,
        "red_card_lines": sum(1 for line in lines if red_ids.intersection(line.event_ids)),
        "possession_pairs": [
            {"text": text, "pair": [a, b], "sums_to_100": round(a + b, 6) == 100}
            for text, a, b in pairs
        ],
        "banned_phrase_rejections": {
            "rejected": len(banned),
            "commentator_calls": commentator_calls,
            "share": round(len(banned) / commentator_calls, 3) if commentator_calls else 0.0,
            "hits": banned,
        },
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
        "lines_emitted": [
            {
                "path": outcome.path,
                "minute": outcome.minute,
                "source": outcome.result.line.source,  # type: ignore[union-attr]
                "delivery_tier": outcome.result.line.delivery_tier,  # type: ignore[union-attr]
                "text": outcome.result.line.text,  # type: ignore[union-attr]
            }
            for outcome in sorted(outcomes, key=lambda item: item.event.seq)
            if outcome.result is not None
            and outcome.result.status == "commentary"
            and outcome.result.line is not None
        ],
        "agent_share": {
            "agent_lines": sum(
                1 for outcome in lines if outcome.result.line.source == "agent"  # type: ignore[union-attr]
            ),
            "lines": len(lines),
        },
        "output_failures": [
            {"path": outcome.path, "minute": outcome.minute, "failure": failure}
            for outcome in sorted(outcomes, key=lambda item: item.event.seq)
            if outcome.result is not None
            for failure in outcome.result.output_failures
        ],
        "agent_calls_by_role": summarize_calls(outcomes),
        "evidence_failures": summarize_failures(outcomes),
        "tokens": summarize_tokens(outcomes),
        "wording_checks": wording_checks(info, events, outcomes),
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
    failures = report["evidence_failures"]
    print("Evidence-check failures by reason:", flush=True)
    for rule, count in failures["by_reason"].items():  # type: ignore[index, union-attr]
        print(f"  {count:3d}  {rule}", flush=True)
    if not failures["by_reason"]:  # type: ignore[index]
        print("  none", flush=True)
    tokens = report["tokens"]
    print(
        f"Tokens: input {tokens['input_tokens']}, output {tokens['output_tokens']}, "  # type: ignore[index]
        f"calls without usage {tokens['calls_without_usage']}",  # type: ignore[index]
        flush=True,
    )
    return report
