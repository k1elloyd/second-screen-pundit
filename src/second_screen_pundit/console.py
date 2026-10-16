from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from second_screen_pundit.agents.commentator import (
    Commentator,
    build_console_insight,
)
from second_screen_pundit.agents.tools.analytics import big_chance_threshold
from second_screen_pundit.engine.generate import load_or_generate_match
from second_screen_pundit.engine.replay import Replay
from second_screen_pundit.engine.schema import Event


def _commentary_events(events: list[Event]) -> list[Event]:
    goal_shots = {
        event_id
        for event in events
        if event.type == "goal"
        for event_id in event.related_event_ids
    }
    return [
        event
        for event in events
        if event.type in {"goal", "card", "substitution", "formation_change"}
        or (
            event.type == "shot"
            and event.xg is not None
            and event.xg > big_chance_threshold()
            and event.event_id not in goal_shots
        )
    ]


async def run_console(match_id: str, speed: int) -> None:
    load_dotenv()
    required = ("FOUNDRY_PROJECT_ENDPOINT", "MODEL_FAST")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Set these variables in the root .env or process environment: "
            + ", ".join(missing)
        )

    info, events = load_or_generate_match(match_id, Path("data/matches"))
    replay = Replay(events, speed)
    targets = {event.event_id: event for event in _commentary_events(events)}
    print(f"Starting synthetic {match_id} replay at {speed}x: {info.home.name} v {info.away.name}")

    with DefaultAzureCredential() as credential:
        commentator = Commentator(
            endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            deployment=os.environ["MODEL_FAST"],
            credential=credential,
        )
        replay.start()
        async for event in replay.stream():
            target = targets.get(event.event_id)
            if target is None:
                continue
            insight = build_console_insight(target, info, events)
            line = await commentator.commentate(insight, target)
            print(line.model_dump_json())
    print(f"Replay complete: {match_id}; {len(events)} events.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a synthetic match and its console commentary.")
    parser.add_argument("match_id", choices=("m01", "m02", "m03"))
    parser.add_argument("--speed", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(run_console(args.match_id, args.speed))


if __name__ == "__main__":
    main()
