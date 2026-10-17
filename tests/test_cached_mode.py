"""Cached mode (also what the public site falls back to): each pre-generated line once."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event
from tests.test_api import _settings


async def _source(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        await asyncio.sleep(0)
        yield event


def _run_cached(match_id: str) -> tuple[LiveDispatcher, list[str], list[str]]:
    settings = replace(_settings(), mode="cached")
    events = generate_match(match_id)
    published: list[str] = []
    prepared: list[str] = []
    dispatcher = LiveDispatcher(
        PunditPipeline(settings, None),
        get_match_info(match_id),
        events,
        settings,
        delay=BroadcastDelay(0),
        on_outcome=lambda outcome: published.append(outcome.result.line.line_id)
        if outcome.result is not None and outcome.result.line is not None
        else None,
        on_line_ready=lambda outcome: prepared.append(outcome.result.line.line_id),
    )
    asyncio.run(dispatcher.run(_source(events)))
    return dispatcher, published, prepared


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_cached_mode_publishes_each_line_once(match_id: str) -> None:
    _, published, _ = _run_cached(match_id)
    repeated = {line_id: count for line_id, count in Counter(published).items() if count > 1}
    assert published, "cached mode published no lines"
    assert repeated == {}


@pytest.mark.parametrize("match_id", ["m01", "m02"])
def test_cached_lines_get_early_audio_and_count_as_lines(match_id: str) -> None:
    dispatcher, published, prepared = _run_cached(match_id)
    # Every published cached line had its audio started before release, like a live line.
    assert prepared == published
    with_lines = [outcome for outcome in dispatcher.outcomes if outcome.result and outcome.result.line]
    assert all(outcome.released_late_ms is not None for outcome in with_lines)
