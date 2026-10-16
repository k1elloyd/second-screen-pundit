from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from second_screen_pundit.agents.cache import load_cached_match
from second_screen_pundit.agents.commentator import validate_plain_spoken_text
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.latency import run_live_latency_gate
from second_screen_pundit.agents.pipeline import (
    PunditPipeline,
    _cached_tool_results,
)
from second_screen_pundit.agents.speech import SpeechSynthesizer
from second_screen_pundit.engine.generate import generate_match, get_match_info


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
                input_event_ids=line.event_ids,
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
        help="Replay a match through the live dispatcher and report per-path latency.",
    )
    parser.add_argument("--match", choices=("m01", "m02", "m03"), default="m01")
    parser.add_argument("--speed", type=int, default=20)
    args = parser.parse_args()
    if args.live_latency:
        asyncio.run(run_live_latency_gate(args.match, args.speed))
    else:
        asyncio.run(run_phase2_gate(args.output))


if __name__ == "__main__":
    main()
