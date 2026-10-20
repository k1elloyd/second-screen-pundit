"""A tactical or colour line about a window long gone is dropped, not published out of order."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from fastapi import Request

from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import LiveWindowResult
from second_screen_pundit.api import STALE_LINE_MINUTES, create_app, is_stale_line
from tests.test_api import _settings
from tests.test_review3_fixes import _line, _publish
from tests.test_stream_resume import _TOKENS, _Connection, _faster_clock, _scope, _stream

# Real seconds a tactical window takes. This replay runs at about 0.3 s a match minute, so a
# tactical line is ready about 10 match minutes after its window, after later fast-path lines
# (m01: the 57' window after the 61' goal, the 72' window after the 81' equaliser).
TACTICAL_SECONDS = 3.0


def test_only_tactical_and_colour_lines_more_than_two_minutes_behind_are_stale() -> None:
    assert STALE_LINE_MINUTES == 2
    assert not is_stale_line("tactical", 24, None)
    assert not is_stale_line("tactical", 27, 29)
    assert is_stale_line("tactical", 24, 29)
    # Fast-path lines (goals, red cards, substitutions…) are never dropped.
    assert not is_stale_line("fast_path", 10, 60)


class _SlowTacticalPipeline:
    """Fast-path lines at once; tactical lines TACTICAL_SECONDS later."""

    def __init__(self) -> None:
        self.lines: dict[str, str] = {}

    async def comment_on_window(self, info, events, window, *, as_of: int, signals=(), **_kwargs):
        tactical = bool(signals)
        if tactical:
            await asyncio.sleep(TACTICAL_SECONDS)
        event = next(item for item in events if item.seq == as_of)
        line_id = f"{info.match_id}-{'t' if tactical else 'f'}{as_of}"
        self.lines[line_id] = "tactical" if tactical else "fast_path"
        line = CommentaryLine(
            line_id=line_id,
            text="A line.",
            insight_id=f"{line_id}-i",
            tone="measured",
            event_ids=[event.event_id],
            delivery_tier="tactical" if tactical else "goal",
        )
        return LiveWindowResult("commentary", line, None, (), None)


def test_a_late_tactical_line_is_dropped_and_fast_path_lines_never_are(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="second_screen_pundit")
    _faster_clock(monkeypatch, 10)
    pipeline = _SlowTacticalPipeline()
    retracted: list[str] = []
    original_retract = LiveDispatcher.retract_line

    def retract_line(self, line_id: str) -> None:
        retracted.append(line_id)
        original_retract(self, line_id)

    monkeypatch.setattr(LiveDispatcher, "retract_line", retract_line)
    settings = replace(_settings(delay=0.2), mode="cached")
    app = create_app(settings=settings, pipeline=pipeline)  # type: ignore[arg-type]
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    async def run() -> tuple[str, list]:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.180"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            _TOKENS[started["replay_id"]] = started["replay_token"]
            connection = _Connection(await _stream(routes, ip, started["replay_id"]))
            messages = await connection.read(
                # Full time: this fake has no recap, so the page gets the recap error.
                lambda got: any(name in {"recap", "error"} for _, name, _ in got), timeout=40
            )
            await connection.drop()
            return started["replay_id"], messages

    replay_id, messages = asyncio.run(asyncio.wait_for(run(), 50))
    published = [
        payload["line"]["line_id"].removeprefix(f"{replay_id}-")
        for _, name, payload in messages
        if name == "commentary"
    ]
    tactical_written = [key for key, path in pipeline.lines.items() if path == "tactical"]
    fast_written = [key for key, path in pipeline.lines.items() if path == "fast_path"]
    dropped = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("stale_line_dropped")
    ]

    assert tactical_written and dropped, "no tactical line was late enough to be dropped"
    assert set(fast_written) <= set(published), "a fast-path line was dropped"
    assert len(published) == len(pipeline.lines) - len(dropped)
    dropped_ids = [message.split("line_id=")[1].split()[0] for message in dropped]
    for line_id in dropped_ids:
        assert pipeline.lines[line_id] == "tactical" and line_id not in published
    # Every dropped line is taken out of the dispatcher's recent lines.
    assert retracted == dropped_ids


def test_a_dropped_line_no_longer_counts_as_said(monkeypatch: pytest.MonkeyPatch) -> None:
    import second_screen_pundit.agents.dispatcher as dispatcher_module
    from second_screen_pundit.engine.generate import generate_match, get_match_info

    first = "Port Calder have more of the ball. Port Calder FC lead 2-1."
    second = "Port Calder press high now. Port Calder lead 2-1."

    # Control: the first line was seen, so the second loses its repeated score.
    published = _publish(
        monkeypatch, [_line(1, first, "tactical"), _line(2, second, "tactical")], window=30
    )
    assert published[1] == "Port Calder press high now."

    # The first line is dropped as stale before the second is written: the second keeps its
    # score, and the first is gone from the lines the clock budget and the Commentator see.
    monkeypatch.setattr(dispatcher_module, "REPEATED_SCORE_SECONDS", 30)
    events = generate_match("m01")
    lines = [_line(1, first, "tactical"), _line(2, second, "tactical")]

    class _Queued:
        async def comment_on_window(self, *_args, **kwargs) -> LiveWindowResult:
            prompts.append(kwargs)
            return LiveWindowResult("commentary", lines.pop(0), None, (), None)

    prompts: list[dict] = []
    dispatcher = LiveDispatcher(
        _Queued(), get_match_info("m01"), events, _settings()  # type: ignore[arg-type]
    )

    async def run() -> None:
        from second_screen_pundit.agents.dispatcher import WindowOutcome

        await dispatcher._run(WindowOutcome("tactical", events[100], 20), [events[100]], (), None)
        assert dispatcher._recent_lines == [first]
        dispatcher.retract_line("m01-l001")
        await dispatcher._run(WindowOutcome("tactical", events[101], 21), [events[101]], (), None)

    asyncio.run(run())
    assert dispatcher.outcomes[1].result.line.text == second  # type: ignore[union-attr]
    assert prompts[1]["recent_lines"] == ()
    assert dispatcher._recent_lines == [second]
    assert [entry[1] for entry in dispatcher._published_at] == [second]


# Every source but the fast path, and the half-time summary.


def test_every_line_source_but_the_fast_path_is_under_the_stale_rule() -> None:
    from typing import get_args, get_type_hints

    from second_screen_pundit.agents.dispatcher import WindowOutcome

    # Tactical windows, cached lines and colour lines are all published as "tactical"; the
    # only other path is the fast path (goals, cards, substitutions, shape changes, big chances).
    paths = set(get_args(get_type_hints(WindowOutcome)["path"]))
    assert paths == {"fast_path", "tactical"}
    assert [path for path in paths if is_stale_line(path, 44, 74)] == ["tactical"]


def _colour_versions(match_id: str, as_of: int) -> list[str]:
    from second_screen_pundit.agents.colour import build_colour_candidate
    from second_screen_pundit.engine.generate import generate_match, get_match_info

    events, info = generate_match(match_id), get_match_info(match_id)
    used: set[str] = set()
    for ordinal in range(1, 12):
        candidate = build_colour_candidate(
            info, events, as_of=as_of, ordinal=ordinal, used_versions=used, used_statistics=set()
        )
        if candidate is None:
            break
        used.add(candidate.version)
    return sorted(used)


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_a_half_time_summary_is_offered_at_the_break_and_never_in_the_second_half(
    match_id: str,
) -> None:
    from second_screen_pundit.engine.generate import generate_match

    events = generate_match(match_id)
    break_as_of = max(event.seq for event in events if event.period == 1)
    assert {"possession-first-half-v1", "shots-first-half-v1"} <= set(
        _colour_versions(match_id, break_as_of)
    )
    for minute in range(46, 91, 4):
        as_of = max(event.seq for event in events if event.minute + event.added_minute <= minute)
        assert not [v for v in _colour_versions(match_id, as_of) if "first-half" in v], minute


def test_a_half_time_summary_ready_after_the_kickoff_is_dropped() -> None:
    from second_screen_pundit.agents.colour import build_colour_candidate
    from second_screen_pundit.api import is_late_half_time_summary
    from second_screen_pundit.engine.generate import generate_match, get_match_info

    events, info = generate_match("m01"), get_match_info("m01")
    break_as_of = max(event.seq for event in events if event.period == 1)
    kickoff = min(event.seq for event in events if event.period == 2)
    candidates = []
    used: set[str] = set()
    for ordinal in range(1, 12):
        candidate = build_colour_candidate(
            info, events, as_of=break_as_of, ordinal=ordinal, used_versions=used,
            used_statistics=set(),
        )
        if candidate is None:
            break
        used.add(candidate.version)
        candidates.append(candidate)
    summary = next(c for c in candidates if c.version == "possession-first-half-v1")
    match_line = next(c for c in candidates if c.version == "possession-match-v1")
    # Before the second half kicks off on the viewer's stream it goes out; after, it's dropped.
    assert not is_late_half_time_summary(summary, kickoff - 1, events)
    assert is_late_half_time_summary(summary, kickoff, events)
    assert is_late_half_time_summary(summary, max(e.seq for e in events), events)
    # Other colour lines are left to the ordinary stale rule.
    assert not is_late_half_time_summary(match_line, kickoff, events)
