import asyncio
from collections.abc import AsyncIterator

import pytest

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match
from second_screen_pundit.engine.schema import Event


async def _slow_source(events: list[Event], gap: float) -> AsyncIterator[Event]:
    for event in events:
        yield event
        await asyncio.sleep(gap)


def test_viewer_stream_releases_each_event_after_the_delay_and_in_order() -> None:
    events = generate_match("m01")[:4]
    delay = BroadcastDelay(0.15)

    async def run() -> list[Event]:
        return [event async for event in delay.events(_slow_source(events, 0.02))]

    released = asyncio.run(run())

    assert [event.seq for event in released] == [event.seq for event in events]
    for event in events:
        assert delay.released[event.seq] - delay.seen_at(event.seq) >= 0.15


def test_events_are_stamped_on_arrival_even_while_earlier_events_are_held() -> None:
    events = generate_match("m01")[:3]
    delay = BroadcastDelay(0.3)
    arrival: dict[int, float] = {}

    async def source() -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        for event in events:
            arrival[event.seq] = loop.time()
            yield event
            await asyncio.sleep(0.01)

    async def run() -> None:
        async for _ in delay.events(source()):
            pass

    asyncio.run(run())

    for event in events:
        assert abs(delay.seen_at(event.seq) - arrival[event.seq]) < 0.05


def test_commentary_waits_for_its_events_release_and_reports_lateness() -> None:
    event = generate_match("m01")[0]
    delay = BroadcastDelay(0.1)

    async def run() -> tuple[float, float]:
        delay.stamp(event)
        early = await delay.wait_released(event.seq)
        await asyncio.sleep(0.05)
        late = await delay.wait_released(event.seq)
        return early, late

    early, late = asyncio.run(run())

    assert early == 0.0
    assert late >= 0.04


def test_agent_clock_is_independent_of_the_viewer_delay() -> None:
    events = generate_match("m01")[:2]
    delay = BroadcastDelay(5)
    delay.stamp(events[0])

    assert delay.release_at(events[0].seq) - delay.seen_at(events[0].seq) == 5


def test_zero_delay_releases_immediately_and_negative_delay_is_rejected() -> None:
    event = generate_match("m01")[0]
    delay = BroadcastDelay(0)
    delay.stamp(event)

    assert asyncio.run(delay.wait_released(event.seq)) >= 0
    with pytest.raises(ValueError):
        BroadcastDelay(-1)


def test_new_settings_default_and_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BROADCAST_DELAY_SECONDS", "TACTICAL_COOLDOWN_MINUTES", "COMMENTATOR_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    defaults = AgentSettings.from_environment()

    assert defaults.broadcast_delay_seconds == 10
    assert defaults.tactical_cooldown_match_minutes == 10
    assert defaults.commentator_reasoning_effort == "low"
    assert defaults.live_timeout_seconds == 15

    monkeypatch.setenv("BROADCAST_DELAY_SECONDS", "0")
    monkeypatch.setenv("COMMENTATOR_REASONING_EFFORT", "none")
    overridden = AgentSettings.from_environment()
    assert overridden.broadcast_delay_seconds == 0
    assert overridden.commentator_reasoning_effort == "none"

    monkeypatch.setenv("COMMENTATOR_REASONING_EFFORT", "high")
    with pytest.raises(ValueError):
        AgentSettings.from_environment()
