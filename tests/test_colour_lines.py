from __future__ import annotations

import pytest

from second_screen_pundit.agents.colour import (
    COLOUR_FIRST_MINUTE,
    COLOUR_IDLE_REAL_SECONDS,
    COLOUR_LAST_MINUTE,
    COLOUR_SPACING_MATCH_MINUTES,
    COLOUR_SPACING_REAL_SECONDS,
    ColourLineGate,
    ColourStatistic,
    build_colour_candidate,
)
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.agents.spoken import to_spoken
from second_screen_pundit.engine.generate import generate_match, get_match_info


def _pipeline() -> PunditPipeline:
    settings = AgentSettings(
        mode="off",
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
    return PunditPipeline(settings, None)


@pytest.mark.parametrize("speed", [5, 20])
def test_viewer_silence_starts_after_the_real_line_queue_empties(speed: int) -> None:
    gate = ColourLineGate()
    gate.update_viewer_queue(False, 0)
    gate.record_real_line(1, audio_enabled=True)
    gate.update_viewer_queue(True, 1)

    def match_minute(now: float) -> float:
        return 30 + (now - 1) * speed / 60

    assert not gate.is_due(
        20,
        match_minute(20),
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    gate.update_viewer_queue(False, 20)
    assert not gate.is_due(
        20 + COLOUR_IDLE_REAL_SECONDS - 0.001,
        match_minute(20 + COLOUR_IDLE_REAL_SECONDS - 0.001),
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    assert gate.is_due(
        20 + COLOUR_IDLE_REAL_SECONDS,
        match_minute(20 + COLOUR_IDLE_REAL_SECONDS),
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )


@pytest.mark.parametrize("speed", [5, 20])
def test_colour_minutes_are_spread_across_both_halves(speed: int) -> None:
    gate = ColourLineGate()
    gate.update_viewer_queue(False, 0)
    start_minute = COLOUR_FIRST_MINUTE - speed * COLOUR_IDLE_REAL_SECONDS / 60
    now = 0.0
    releases: list[float] = []
    events = generate_match("m01")
    info = get_match_info("m01")
    used_versions: set[str] = set()
    used_statistics: set[str] = set()
    candidates = []

    for index, target_minute in enumerate((10, 25, 40, 55, 70, 85)):
        silence_due = (gate.silence_started_at or 0) + COLOUR_IDLE_REAL_SECONDS
        match_due = (target_minute - start_minute) * 60 / speed
        spacing_due = (
            (gate.last_colour_at or 0) + COLOUR_SPACING_REAL_SECONDS
            if gate.last_colour_at is not None
            else 0
        )
        now = max(now, silence_due, match_due, spacing_due)
        minute = start_minute + now * speed / 60
        assert minute == pytest.approx(target_minute)
        assert gate.is_due(
            now,
            minute,
            event_pending=False,
            stream_open=True,
            replay_running=True,
        )
        as_of = next(
            event.seq
            for event in events
            if event.minute + event.added_minute >= target_minute
        )
        candidate = build_colour_candidate(
            info,
            events,
            as_of=as_of,
            ordinal=index + 1,
            used_versions=used_versions,
            used_statistics=used_statistics,
        )
        assert candidate is not None
        gate.record_colour_release(
            candidate.version,
            candidate.statistic_keys,
            minute,
            now,
            audio_enabled=True,
        )
        used_versions.add(candidate.version)
        used_statistics.update(candidate.statistic_keys)
        candidates.append(candidate)
        releases.append(minute)
        now += 5
        gate.update_viewer_queue(False, now)

    assert releases == pytest.approx([10, 25, 40, 55, 70, 85])
    assert any("second_half" in key for candidate in candidates for key in candidate.statistic_keys)
    assert all(
        later - earlier >= COLOUR_SPACING_MATCH_MINUTES
        for earlier, later in zip(releases, releases[1:])
    )
    assert all(COLOUR_FIRST_MINUTE <= minute <= COLOUR_LAST_MINUTE for minute in releases)


def test_colour_gate_enforces_real_and_match_time_spacing_and_event_windows() -> None:
    gate = ColourLineGate()
    gate.update_viewer_queue(False, 0)

    assert not gate.is_due(
        12,
        9.99,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    assert not gate.is_due(
        12,
        10,
        event_pending=True,
        stream_open=True,
        replay_running=True,
    )
    assert not gate.is_due(
        12,
        10,
        event_pending=False,
        stream_open=False,
        replay_running=True,
    )
    assert gate.is_due(
        12,
        10,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )

    gate.record_colour_release("possession-v1", ("possession:first-half",), 10, 12, audio_enabled=False)
    assert not gate.is_due(
        12 + COLOUR_SPACING_REAL_SECONDS - 0.001,
        25,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    assert gate.is_due(
        12 + COLOUR_SPACING_REAL_SECONDS,
        25,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    assert not gate.is_due(
        12 + COLOUR_SPACING_REAL_SECONDS,
        24.99,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )
    assert not gate.is_due(
        12 + COLOUR_SPACING_REAL_SECONDS,
        85.01,
        event_pending=False,
        stream_open=True,
        replay_running=True,
    )


def test_colour_templates_are_unique_and_pass_the_existing_line_checks() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    pipeline = _pipeline()
    used_versions: set[str] = set()
    used_statistics: set[str] = set()
    candidates = []

    while candidate := build_colour_candidate(
        info,
        events,
        as_of=events[-1].seq,
        ordinal=len(candidates) + 1,
        used_versions=used_versions,
        used_statistics=used_statistics,
    ):
        assert candidate.version not in used_versions
        assert not set(candidate.statistic_keys).intersection(used_statistics)
        result = pipeline.validate_colour_line(
            candidate.line,
            candidate.insight,
            info=info,
            events=events,
            as_of=events[-1].seq,
            tool_results=list(candidate.tool_results),
            lane=candidate.lane,
        )
        assert result.status == "commentary"
        assert result.line is not None and result.line.source == "colour"
        assert result.line.delivery_tier == "routine"
        assert result.evidence is not None and result.evidence.passed
        assert to_spoken(result.line.text)
        candidates.append(candidate)
        used_versions.add(candidate.version)
        used_statistics.update(candidate.statistic_keys)

    assert candidates
    assert len(used_versions) == len(candidates)
    assert len(used_statistics) == len(candidates)
    category_versions: dict[ColourStatistic, int] = {}
    for candidate in candidates:
        category_versions[candidate.statistic] = (
            category_versions.get(candidate.statistic, 0) + 1
        )
    assert category_versions["possession"] >= 2
    assert category_versions["shots_on_target"] >= 2


def test_second_half_templates_are_available_at_match_snapshots() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    used_versions: set[str] = set()
    used_statistics: set[str] = set()
    second_half_candidates = []

    for minute in (55, 70, 85):
        as_of = next(
            event.seq
            for event in events
            if event.minute + event.added_minute >= minute
        )
        candidate = build_colour_candidate(
            info,
            events,
            as_of=as_of,
            ordinal=len(second_half_candidates) + 1,
            used_versions=used_versions,
            used_statistics=used_statistics,
        )
        if candidate is not None:
            assert candidate.version not in used_versions
            assert not set(candidate.statistic_keys).intersection(used_statistics)
            second_half_candidates.append(candidate)
            used_versions.add(candidate.version)
            used_statistics.update(candidate.statistic_keys)
    assert second_half_candidates
    assert all(candidate.line.source == "colour" for candidate in second_half_candidates)
