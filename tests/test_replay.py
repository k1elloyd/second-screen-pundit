import asyncio

import pytest

from second_screen_pundit.engine.replay import Replay, ReplayManager
from second_screen_pundit.engine.schema import Event


def _events() -> list[Event]:
    return [
        Event(
            event_id=f"m01-e{seq:04d}",
            match_id="m01",
            seq=seq,
            period=1,
            minute=0,
            second=0,
            team=team,
            type="kickoff",
            detail={"formation": "4-3-3"},
        )
        for seq, team in enumerate(("home", "away", "home"), start=1)
    ]


async def _collect(replay: Replay) -> list[Event]:
    return [event async for event in replay.stream()]


def test_replay_pause_resume_preserves_order_and_as_of() -> None:
    async def scenario() -> None:
        replay = Replay(_events(), speed=60)
        second_reader = replay.subscribe()
        replay.start()
        assert await replay.control("pause") == "paused"
        await asyncio.sleep(0.01)
        assert replay.as_of == 0

        assert await replay.control("resume") == "running"
        delivered = await asyncio.wait_for(_collect(replay), timeout=10)
        second_delivered = [event async for event in second_reader]
        assert [event.seq for event in delivered] == [1, 2, 3]
        assert [event.seq for event in second_delivered] == [1, 2, 3]
        assert replay.as_of == 3
        assert replay.state == "finished"

    asyncio.run(scenario())


def test_replay_speed_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 120"):
        Replay(_events(), speed=121)
    with pytest.raises(ValueError, match="between 1 and 120"):
        Replay(_events(), speed=0)


def test_replay_manager_enforces_concurrency_limit_and_control_no_ops() -> None:
    async def scenario() -> None:
        manager = ReplayManager(max_concurrent=3)
        replays = [manager.start(_events(), speed=60) for _ in range(3)]
        with pytest.raises(RuntimeError, match="Maximum concurrent"):
            manager.start(_events(), speed=60)

        assert await replays[0].control("pause") == "paused"
        assert await replays[0].control("pause") == "paused"
        assert await replays[0].control("resume") == "running"
        for replay in replays:
            await replay.stop()

    asyncio.run(scenario())
