"""Polish session, group 2: possession adds up to 100, and one line per red card."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.agents.tools.analytics import (
    compare_windows,
    match_snapshot,
    possession_pair,
    possession_window,
)
from second_screen_pundit.agents.tools.parameters import (
    CompareWindowsParams,
    PossessionWindowParams,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event


def _settings() -> AgentSettings:
    return AgentSettings(
        mode="live",
        endpoint=None,
        model_fast=None,
        model_deep=None,
        speech_endpoint=None,
        speech_region=None,
        live_timeout_seconds=15,
        explainer_timeout_seconds=20,
        recap_timeout_seconds=45,
        importance_threshold=0.45,
        cooldown_match_minutes=4,
    )


def _spoken(fact) -> float:  # type: ignore[no-untyped-def]
    return fact.spoken_value if fact.spoken_value is not None else fact.value


@pytest.mark.parametrize(
    ("home", "spoken"),
    [(60.5, (61, 39)), (60.45, (61, 39)), (52.7, (53, 47)), (33.333, (33, 67)), (50.0, (50, 50))],
)
def test_possession_pair_rounds_to_100(home: float, spoken: tuple[int, int]) -> None:
    pair = possession_pair(home)
    assert (pair["home"][1], pair["away"][1]) == spoken
    assert round(pair["home"][0] + pair["away"][0], 6) == 100


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_every_possession_pair_sums_to_100_in_windows_and_snapshots(match_id: str) -> None:
    events = generate_match(match_id)
    checked = 0
    for as_of in range(200, events[-1].seq, 150):
        for start in range(0, 81, 10):
            result = possession_window(
                events, PossessionWindowParams(start=start, end=start + 10), as_of=as_of
            )
            home, away = result.facts
            if home.value == away.value == 0.0:
                continue
            assert round(home.value + away.value, 6) == 100, (home, away)  # type: ignore[operator]
            assert _spoken(home) + _spoken(away) == 100, (home, away)
            checked += 1
        snapshot = match_snapshot(events, as_of=as_of)[0]
        possession = [fact for fact in snapshot.facts if " possession" in fact.label]
        for whole_match in (True, False):
            pair = [fact for fact in possession if ("through" in fact.label) == whole_match]
            assert len(pair) == 2
            if pair[0].value or pair[1].value:
                assert _spoken(pair[0]) + _spoken(pair[1]) == 100, pair
    assert checked > 30


def test_compare_windows_uses_the_same_pair_rounding() -> None:
    events = generate_match("m01")
    as_of = events[-1].seq
    for team in ("home", "away"):
        params = CompareWindowsParams.model_validate(
            {"team": team, "metric": "possession",
             "before": {"start": 0, "end": 45}, "after": {"start": 45, "end": 90}}
        )
        before = compare_windows(events, params, as_of=as_of).facts[0]
        window = possession_window(
            events, PossessionWindowParams(start=0, end=45), as_of=as_of
        ).facts[0 if team == "home" else 1]
        assert (before.value, before.spoken_value) == (window.value, window.spoken_value)


def test_m02_has_exactly_one_red_card_line(monkeypatch: pytest.MonkeyPatch) -> None:
    info, events = get_match_info("m02"), generate_match("m02")
    red = next(e for e in events if e.type == "card" and e.detail.get("colour") == "red")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class FailingAgent:
        async def run(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(value=None)

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: FailingAgent())

    async def feed() -> AsyncIterator[Event]:
        for event in events:
            yield event
            await asyncio.sleep(0)

    dispatcher = LiveDispatcher(pipeline, info, events, _settings())
    asyncio.run(dispatcher.run(feed()))
    published = [
        outcome.result.line
        for outcome in dispatcher.outcomes
        if outcome.result is not None and outcome.result.line is not None
    ]
    red_lines = [line for line in published if red.event_id in line.event_ids]
    assert len(red_lines) == 1, [line.text for line in red_lines]
    skipped = [
        outcome
        for outcome in dispatcher.outcomes
        if outcome.result is not None
        and outcome.result.notice == "This red card was already announced by the other lane."
    ]
    assert len(skipped) == 1
