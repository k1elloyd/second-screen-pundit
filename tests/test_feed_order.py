"""A tactical or colour line about a window long gone is dropped, not published out of order."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from fastapi import Request

from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import LiveWindowResult
from second_screen_pundit.api import STALE_LINE_MINUTES, create_app, is_stale_line
from tests.test_api import _settings
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
    for message in dropped:
        line_id = message.split("line_id=")[1].split()[0]
        assert pipeline.lines[line_id] == "tactical" and line_id not in published
