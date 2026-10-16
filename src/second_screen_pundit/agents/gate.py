from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Literal

from agent_framework import Agent
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from second_screen_pundit.agents.cache import load_cached_match
from second_screen_pundit.agents.commentator import validate_plain_spoken_text
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.pipeline import (
    LiveWindowResult,
    PunditPipeline,
    _cached_tool_results,
    is_must_say,
)
from second_screen_pundit.agents.clients import create_foundry_chat_client
from second_screen_pundit.agents.speech import SpeechSynthesizer
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay
from second_screen_pundit.engine.schema import Event


@dataclass(frozen=True)
class _WindowMeasurement:
    path: Literal["fast_path", "tactical"]
    result: LiveWindowResult | None = None
    dropped_reason: str | None = None


def _window_summary(
    measurements: list[_WindowMeasurement],
    path: Literal["fast_path", "tactical"],
) -> dict[str, object]:
    selected = [item for item in measurements if item.path == path]
    lines = [
        item.result
        for item in selected
        if item.result is not None
        and item.result.status == "commentary"
        and item.result.line is not None
    ]
    durations = [
        sum(
            timing.duration_ms
            for timing in result.timings
            if timing.role == "commentator"
        )
        for result in lines
    ]
    dropped = [
        item
        for item in selected
        if item.result is None
        or item.result.status != "commentary"
        or item.result.line is None
    ]
    reasons: Counter[str] = Counter()
    for item in dropped:
        if item.dropped_reason:
            reason = item.dropped_reason
        elif item.result is None:
            reason = "window was not dispatched"
        elif item.result.notice:
            reason = item.result.notice
        elif item.result.status == "skipped":
            reason = "Director chose not to comment"
        elif item.result.evidence is not None and item.result.evidence.failures:
            reason = "Evidence check failed: " + "; ".join(item.result.evidence.failures)
        else:
            reason = item.result.status
        reasons[reason] += 1
    return {
        "windows": len(selected),
        "lines": len(lines),
        "agent_calls": [
            {
                "role": timing.role,
                "attempt": timing.attempt,
                "duration_ms": timing.duration_ms,
            }
            for item in selected
            if item.result is not None
            for timing in item.result.timings
        ],
        "commentator_ms_per_line": {
            "count": len(durations),
            "median": median(durations) if durations else None,
            "slowest": max(durations) if durations else None,
        },
        "dropped_lines": len(dropped),
        "drop_reasons": dict(reasons),
    }


async def _warm_foundry(
    settings: AgentSettings,
    credential: DefaultAzureCredential,
) -> dict[str, int]:
    endpoint, model_fast, _ = settings.require_foundry()
    warmup_started = perf_counter()
    token_started = perf_counter()
    credential.get_token("https://ai.azure.com/.default")
    token_ms = round((perf_counter() - token_started) * 1000)

    client = create_foundry_chat_client(
        endpoint=endpoint,
        deployment=model_fast,
        credential=credential,
        time_budget_seconds=settings.live_timeout_seconds,
    )
    agent = Agent(
        client=client,
        instructions="Reply with the single word PONG.",
        default_options={"max_tokens": 16, "store": False},
    )
    model_started = perf_counter()
    response = await asyncio.wait_for(
        agent.run("Reply with the single word PONG."),
        timeout=settings.live_timeout_seconds,
    )
    model_ms = round((perf_counter() - model_started) * 1000)
    if "PONG" not in response.text.upper():
        raise RuntimeError("Foundry warm-up did not return the expected PONG response")
    return {
        "token_ms": token_ms,
        "model_call_ms": model_ms,
        "total_ms": round((perf_counter() - warmup_started) * 1000),
    }


async def run_phase2_gate(output_path: Path) -> None:
    load_dotenv()
    settings = AgentSettings.from_environment()
    if settings.mode == "off":
        raise RuntimeError("The Phase 2 gate requires NARRATIVE_MODE=live or cached")
    speech_endpoint, speech_region = settings.require_speech()
    info = get_match_info("m01")
    events = generate_match("m01")
    goal = next(
        event
        for event in events
        if event.type == "goal" and event.minute == 23
    )

    with DefaultAzureCredential() as credential:
        pipeline = PunditPipeline(
            settings,
            credential if settings.mode == "live" else None,
        )
        result = await pipeline.comment_on_window(
            info,
            events,
            [goal],
            as_of=goal.seq,
        )
        print(
            "Live-window timing: "
            + json.dumps(
                [
                    {
                        "role": timing.role,
                        "attempt": timing.attempt,
                        "duration_ms": timing.duration_ms,
                    }
                    for timing in result.timings
                ]
            )
        )
        if result.line is None or result.evidence is None or not result.evidence.passed:
            failures = (
                "; ".join(result.evidence.failures)
                if result.evidence is not None
                else "no evidence report was returned"
            )
            raise RuntimeError(
                "The Phase 2 transcript did not pass evidence checks: "
                + failures
                + (f" ({result.notice})" if result.notice else "")
            )
        print("Validated transcript:")
        print(result.line.model_dump_json())

        cached = load_cached_match("m01")
        event_by_id = {event.event_id: event for event in events}
        validated_text: list[str] = []
        for line in cached.commentary:
            cited = [event_by_id.get(event_id) for event_id in line.event_ids]
            if not cited or any(event is None for event in cited):
                raise RuntimeError(f"Cached line {line.line_id} cites an unknown event")
            as_of = max(event.seq for event in cited if event is not None)
            facts = _cached_tool_results(
                events,
                as_of=as_of,
                fact_ids=line.fact_ids,
            )
            report = check_evidence(
                line,
                events=events,
                as_of=as_of,
                tool_results=facts,
                info=info,
            )
            if not report.passed:
                raise RuntimeError(
                    f"Cached line {line.line_id} failed evidence checks: "
                    + "; ".join(report.failures)
                )
            validate_plain_spoken_text(line.text)
            validated_text.append(line.text)

        speech = SpeechSynthesizer(
            endpoint=speech_endpoint,
            region=speech_region,
            credential=credential,
        )
        script = " ".join(validated_text + validated_text[:2])
        artifact = speech.synthesize_mp3(script, output_path)
        if artifact.duration_seconds < 120:
            raise RuntimeError(
                "Synthesized commentary is shorter than the required 120 seconds: "
                f"{artifact.duration_seconds:.1f} seconds"
            )
        print(
            f"MP3 verified: {artifact.path} "
            f"({artifact.duration_seconds:.1f} seconds, {artifact.path.stat().st_size} bytes)"
        )


async def run_live_latency_gate() -> None:
    load_dotenv()
    settings = AgentSettings.from_environment()
    if settings.mode != "live":
        raise RuntimeError("The live latency gate requires NARRATIVE_MODE=live")
    info = get_match_info("m01")
    events = generate_match("m01")
    with DefaultAzureCredential() as credential:
        warmup = await _warm_foundry(settings, credential)
        print("Foundry warm-up: " + json.dumps(warmup))

        pipeline = PunditPipeline(settings, credential)
        replay = Replay(events, speed=20)
        measurements: list[_WindowMeasurement] = []
        tasks: set[asyncio.Task[_WindowMeasurement]] = set()
        tactical_events: list[Event] = []
        next_tactical_minute = 3
        generation = 0

        async def dispatch(
            path: Literal["fast_path", "tactical"],
            window: Sequence[Event],
            as_of: int,
            window_generation: int,
        ) -> _WindowMeasurement:
            stale_check = (
                (lambda: generation != window_generation)
                if path == "tactical"
                else None
            )
            result = await pipeline.comment_on_window(
                info,
                events,
                window,
                as_of=as_of,
                is_stale=stale_check,
            )
            return _WindowMeasurement(path=path, result=result)

        async def collect_finished() -> None:
            finished = {task for task in tasks if task.done()}
            if finished:
                measurements.extend(await asyncio.gather(*finished))
                tasks.difference_update(finished)

        replay.start()
        replay_started = perf_counter()
        try:
            async for event in replay.stream():
                current_minute = event.minute + event.added_minute
                must_say = is_must_say(event)
                if not must_say:
                    tactical_events.append(event)

                while current_minute >= next_tactical_minute:
                    if tactical_events:
                        generation += 1
                        tasks.add(
                            asyncio.create_task(
                                dispatch(
                                    "tactical",
                                    list(tactical_events),
                                    event.seq,
                                    generation,
                                )
                            )
                        )
                        tactical_events = []
                    next_tactical_minute += 3

                if must_say:
                    generation += 1
                    tasks.add(
                        asyncio.create_task(
                            dispatch("fast_path", [event], event.seq, generation)
                        )
                    )
                await collect_finished()

            measurements.extend(await asyncio.gather(*tasks))
            tasks.clear()
            replay_elapsed_ms = round((perf_counter() - replay_started) * 1000)
        finally:
            await replay.stop()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    fast_summary = _window_summary(measurements, "fast_path")
    tactical_summary = _window_summary(measurements, "tactical")
    print(
        "Full-match latency gate: "
        + json.dumps(
            {
                "match_id": "m01",
                "speed": replay.speed,
                "budget_ms": round(settings.live_timeout_seconds * 1000),
                "replay_elapsed_ms": replay_elapsed_ms,
                "fast_path": fast_summary,
                "tactical": tactical_summary,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Second Screen Pundit acceptance gate.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/audio/phase2-m01-commentary.mp3"),
    )
    parser.add_argument(
        "--live-latency",
        action="store_true",
        help="Run the m01 live-commentary latency gate at 20x without synthesizing audio.",
    )
    args = parser.parse_args()
    if args.live_latency:
        asyncio.run(run_live_latency_gate())
    else:
        asyncio.run(run_phase2_gate(args.output))


if __name__ == "__main__":
    main()
