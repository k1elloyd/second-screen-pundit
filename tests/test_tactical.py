import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

import pytest

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import LiveWindowResult, is_must_say
from second_screen_pundit.agents.signals import SignalTracker, TacticalSignal
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


def _boundary_events(events: list[Event]) -> list[Event]:
    boundaries: list[Event] = []
    next_boundary = 3
    for event in events:
        clock = event.minute + event.added_minute
        if clock >= next_boundary:
            boundaries.append(event)
            while clock >= next_boundary:
                next_boundary += 3
    return boundaries


def test_formation_change_signal_carries_new_and_previous_formation_and_fires_once() -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    change = next(
        event
        for event in events
        if event.type == "formation_change" and event.detail.get("formation") == "3-4-3"
    )
    tracker = SignalTracker(info)

    signals = tracker.evaluate(events, as_of=change.seq)
    signal = next(
        signal
        for signal in signals
        if signal.kind == "formation_change" and signal.facts[0].value == "3-4-3"
    )

    assert signal.mandatory and signal.team == "home"
    assert [fact.value for fact in signal.facts] == ["3-4-3", "4-2-3-1"]
    assert change.event_id in signal.facts[0].event_ids
    assert not [
        again
        for again in tracker.evaluate(events, as_of=change.seq)
        if again.kind == "formation_change"
    ]


def test_signals_never_cite_events_after_as_of() -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    seq_by_id = {event.event_id: event.seq for event in events}
    tracker = SignalTracker(info)

    for boundary in _boundary_events(events):
        for signal in tracker.evaluate(events, as_of=boundary.seq):
            assert all(seq_by_id[event_id] <= boundary.seq for event_id in signal.event_ids)


def test_signal_fires_only_after_its_event_has_been_seen() -> None:
    info = get_match_info("m02")
    events = generate_match("m02")
    red = next(
        event for event in events if event.type == "card" and event.detail.get("colour") == "red"
    )

    before = SignalTracker(info).evaluate(events, as_of=red.seq - 1)
    after = SignalTracker(info).evaluate(events, as_of=red.seq)

    assert not [signal for signal in before if signal.kind == "red_card"]
    signal = next(signal for signal in after if signal.kind == "red_card")
    assert signal.team == red.team == "home"
    assert signal.facts[0].value == 10 and signal.mandatory


def test_rate_signals_respect_the_configured_thresholds() -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    tracker = SignalTracker(info)
    seen_kinds: set[str] = set()

    for boundary in _boundary_events(events):
        for signal in tracker.evaluate(events, as_of=boundary.seq):
            seen_kinds.add(signal.kind)
            values = {fact.fact_id.rsplit("_", 1)[-1]: fact.value for fact in signal.facts}
            if signal.kind == "possession_swing":
                assert float(values["change"]) > 15
            if signal.kind == "xg_surge":
                own = float(next(f.value for f in signal.facts if f.fact_id.endswith("_xg")
                                 and not f.fact_id.endswith("opponent_xg")))
                opponent = float(next(f.value for f in signal.facts if f.fact_id.endswith("opponent_xg")))
                assert own > 0.4 and own > opponent

    assert {"possession_swing", "xg_surge", "formation_change"} <= seen_kinds


class _StubPipeline:
    """Stands in for the models: records dispatches and honours the stale predicate."""

    def __init__(self, hold_until: asyncio.Event | None = None) -> None:
        self.hold_until = hold_until
        self.calls: list[tuple[int, tuple[TacticalSignal, ...]]] = []

    async def comment_on_window(
        self,
        info: object,
        events: list[Event],
        window: Sequence[Event],
        *,
        as_of: int,
        signals: Sequence[TacticalSignal] = (),
        is_stale: Callable[[], bool] | None = None,
    ) -> LiveWindowResult:
        self.calls.append((as_of, tuple(signals)))
        if self.hold_until is not None and signals:
            await self.hold_until.wait()
        if is_stale is not None and is_stale():
            return LiveWindowResult("skipped", None, None, (), None, "stale window")
        line = CommentaryLine(
            line_id=f"x-l{as_of}",
            text="A line.",
            insight_id="i",
            tone="measured",
            event_ids=[window[-1].event_id],
        )
        return LiveWindowResult("commentary", line, None, (), None)


async def _feed(events: list[Event], on_end: asyncio.Event | None = None) -> AsyncIterator[Event]:
    for event in events:
        yield event
        await asyncio.sleep(0)
    if on_end is not None:
        on_end.set()


def _dispatch(match_id: str, pipeline: _StubPipeline, end: asyncio.Event | None = None) -> LiveDispatcher:
    info, events = get_match_info(match_id), generate_match(match_id)
    dispatcher = LiveDispatcher(pipeline, info, events, _settings())  # type: ignore[arg-type]
    asyncio.run(dispatcher.run(_feed(events, end)))
    return dispatcher


def test_storyline_tactical_beats_each_produce_a_tactical_line() -> None:
    m01 = _dispatch("m01", _StubPipeline())
    back_three = [
        outcome
        for outcome in m01.outcomes
        if outcome.path == "tactical"
        and any(
            signal.kind == "formation_change" and signal.facts[0].value == "3-4-3"
            for signal in outcome.signals
        )
    ]
    m02 = _dispatch("m02", _StubPipeline())
    red_card = [
        outcome
        for outcome in m02.outcomes
        if outcome.path == "tactical"
        and any(signal.kind == "red_card" for signal in outcome.signals)
    ]

    assert len(back_three) == 1 and back_three[0].result.status == "commentary"  # type: ignore[union-attr]
    assert len(red_card) == 1 and red_card[0].result.status == "commentary"  # type: ignore[union-attr]


def test_tactical_lane_rate_is_bounded_by_the_cooldowns() -> None:
    dispatcher = _dispatch("m01", _StubPipeline())
    dispatched = [b for b in dispatcher.boundaries if b.decision == "dispatched"]
    minutes = [b.minute for b in dispatched]
    rate_only = [
        b.minute
        for b in dispatched
        if not {"red_card", "formation_change"} & set(b.signal_kinds)
    ]

    assert all(later - earlier >= 4 for earlier, later in zip(minutes, minutes[1:]))
    assert all(later - earlier >= 10 for earlier, later in zip(rate_only, rate_only[1:]))
    assert 4 <= len(dispatched) <= 9
    assert {b.decision for b in dispatcher.boundaries} <= {
        "no_signal",
        "held_cooldown",
        "held_in_flight",
        "dispatched",
    }


def test_must_say_events_use_the_fast_lane_and_never_the_tactical_lane() -> None:
    dispatcher = _dispatch("m01", _StubPipeline())
    fast = [outcome for outcome in dispatcher.outcomes if outcome.path == "fast_path"]
    tactical = [outcome for outcome in dispatcher.outcomes if outcome.path == "tactical"]
    events = generate_match("m01")

    assert len(fast) == sum(is_must_say(event, events) for event in events)
    assert all(is_must_say(outcome.event, events) for outcome in fast)
    assert tactical and not any(
        is_must_say(outcome.event, events) and outcome.signals == ()
        for outcome in tactical
    )


def test_scoring_shot_and_linked_goal_produce_one_must_say_line() -> None:
    events = generate_match("m01")
    goals = [event for event in events if event.type == "goal"]
    scoring_shot_ids = {
        event_id for goal in goals for event_id in goal.related_event_ids
    }

    assert scoring_shot_ids
    assert all(
        not is_must_say(event, events)
        for event in events
        if event.type == "shot" and event.event_id in scoring_shot_ids
    )
    assert all(is_must_say(goal, events) for goal in goals)
    dispatcher = _dispatch("m01", _StubPipeline())
    fast_event_ids = [outcome.event.event_id for outcome in dispatcher.outcomes
                      if outcome.path == "fast_path"]
    assert len(fast_event_ids) == len(set(fast_event_ids))
    assert not scoring_shot_ids.intersection(fast_event_ids)
    assert all(goal.event_id in fast_event_ids for goal in goals)


def test_high_turnover_surge_is_as_of_safe_and_covers_m03_storyline() -> None:
    info = get_match_info("m03")
    events = generate_match("m03")
    tracker = SignalTracker(info)
    seq_by_id = {event.event_id: event.seq for event in events}
    found = []

    for boundary in _boundary_events(events):
        signals = tracker.evaluate(events, as_of=boundary.seq)
        surges = [signal for signal in signals if signal.kind == "high_turnover_surge"]
        found.extend(surges)
        for signal in surges:
            assert all(seq_by_id[event_id] <= boundary.seq for event_id in signal.event_ids)
            recent = next(fact for fact in signal.facts if fact.fact_id.endswith("_recent"))
            average = next(
                fact for fact in signal.facts if fact.fact_id.endswith("_match_average")
            )
            assert float(recent.value) >= 2 * float(average.value)

    assert found


def test_older_tactical_run_is_dropped_when_a_newer_window_is_dispatched() -> None:
    end = asyncio.Event()
    pipeline = _StubPipeline(hold_until=end)
    dispatcher = _dispatch("m01", pipeline, end)
    tactical = sorted(
        (outcome for outcome in dispatcher.outcomes if outcome.path == "tactical"),
        key=lambda outcome: outcome.event.seq,
    )

    assert len(tactical) >= 2
    assert [outcome.result.status for outcome in tactical[:-1]] == ["skipped"] * (len(tactical) - 1)  # type: ignore[union-attr]
    assert tactical[-1].result.status == "commentary"  # type: ignore[union-attr]


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_every_window_is_dispatched_with_as_of_equal_to_its_own_event(match_id: str) -> None:
    pipeline = _StubPipeline()
    dispatcher = _dispatch(match_id, pipeline)

    assert {outcome.event.seq for outcome in dispatcher.outcomes} == {
        as_of for as_of, _ in pipeline.calls
    }
