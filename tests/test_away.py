"""Mobile session, group 2: leaving and coming back (lock screen, app switch, tab switch).

A dropped stream pauses a running replay at once, so a locked phone doesn't miss the match.
The paused replay is kept for STREAM_PAUSED_GRACE_SECONDS (10 minutes by default; shortened
here) and makes no model or speech calls meanwhile. The stream's own planned close (every
STREAM_MAX_SECONDS) is not a drop: the match keeps running while the page reconnects.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.api import create_app
from tests.test_api import _settings, _SpeechStub
from tests.test_stream_resume import (
    _TOKENS,
    _Connection,
    _faster_clock,
    _json_request,
    _scope,
    _settled,
    _stream,
)


class _CountingPipeline:
    """The cached pipeline, counting every model-shaped call the replay makes."""

    def __init__(self, inner: PunditPipeline) -> None:
        self.inner = inner
        self.calls = 0

    async def comment_on_window(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self.inner.comment_on_window(*args, **kwargs)

    async def recap(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self.inner.recap(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class _CountingSynthesis:
    def __init__(self) -> None:
        self.calls = 0

    def synthesize_mp3(self, _text: str, path: Path, *, tier=None):
        self.calls += 1
        path.write_bytes(b"stub-mp3")
        return SimpleNamespace(path=path)


def _app(settings: AgentSettings, pipeline: Any = None, synthesizer: Any = None):
    app = create_app(
        settings=settings,
        pipeline=pipeline or PunditPipeline(settings, None),
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=synthesizer or _CountingSynthesis(),  # type: ignore[arg-type]
    )
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    return app, routes


async def _start(routes, ip: str) -> str:
    started = await routes["/api/matches/{match_id}/start"](
        "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
    )
    _TOKENS[started["replay_id"]] = started["replay_token"]
    return started["replay_id"]


async def _until(condition, *, limit: float = 10.0, every: float = 0.02) -> None:
    """Wait for a condition, failing after `limit` seconds rather than guessing a sleep."""
    deadline = time.monotonic() + limit
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(every)


def _cached(**overrides: Any) -> AgentSettings:
    return replace(_settings(delay=0.2), mode="cached", **overrides)


def test_a_dropped_stream_pauses_the_replay_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(_cached())

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.110"
            replay_id = await _start(routes, ip)
            session = app.state.sessions[replay_id]
            connection = _Connection(await _stream(routes, ip, replay_id))
            await connection.read(lambda got: sum(n == "event" for _, n, _ in got) >= 20)
            assert session.replay.state == "running"
            as_of = session.replay.as_of
            await connection.drop()
            # Straight away: no grace period in which the match runs on unseen.
            assert session.replay.state == "paused"
            assert session.delay.paused
            await asyncio.sleep(0.5)
            assert session.replay.as_of == as_of, "the match clock ran on after the drop"

    asyncio.run(asyncio.wait_for(run(), 30))


def test_the_planned_close_does_not_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 4)
    app, routes = _app(_cached(stream_max_seconds=0.5))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.111"
            replay_id = await _start(routes, ip)
            session = app.state.sessions[replay_id]
            connection = _Connection(await _stream(routes, ip, replay_id))
            await connection.read(lambda _got: False, timeout=5)
            assert connection.ended  # closed by the server, on schedule
            assert session.replay.state == "running"
            as_of = session.replay.as_of
            await _until(lambda: session.replay.as_of > as_of)  # the match carries on

    asyncio.run(asyncio.wait_for(run(), 30))


def test_a_paused_replay_is_kept_for_the_paused_grace_then_freed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(
        _cached(stream_reconnect_grace_seconds=0.3, stream_paused_grace_seconds=3)
    )

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.112"
            replay_id = await _start(routes, ip)
            connection = _Connection(await _stream(routes, ip, replay_id))
            await connection.read(lambda got: sum(n == "event" for _, n, _ in got) >= 10)
            await connection.drop()
            dropped = time.monotonic()
            # Well past the running replay's 0.3 s grace, the paused one is still there.
            await asyncio.sleep(1.0)
            assert replay_id in app.state.sessions
            await _until(lambda: replay_id not in app.state.sessions, limit=15)
            assert time.monotonic() - dropped >= 3
            # The page then gets a 404, and shows "This replay has ended".
            with pytest.raises(HTTPException) as gone:
                await _stream(routes, ip, replay_id, "1")
            assert gone.value.status_code == 404

    asyncio.run(asyncio.wait_for(run(), 30))


def test_coming_back_within_the_grace_resumes_where_it_left_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(
        _cached(stream_reconnect_grace_seconds=0.3, stream_paused_grace_seconds=5)
    )

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.113"
            replay_id = await _start(routes, ip)
            first = _Connection(await _stream(routes, ip, replay_id))
            before = await first.read(lambda got: sum(n == "commentary" for _, n, _ in got) >= 1)
            await first.drop()
            await asyncio.sleep(1.0)  # longer than the running grace: the pause keeps it
            second = _Connection(await _stream(routes, ip, replay_id, str(before[-1][0])))
            resumed = await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "resume"}, ip, replay_id)
            )
            assert resumed["state"] == "running"
            after = await second.read(lambda got: got[-1][1] == "complete", timeout=60)
            ids = [i for i, _, _ in before + after]
            assert ids == list(range(1, len(ids) + 1)), "gap or duplicate across the lock"
            lines = [p["line"]["line_id"] for _, n, p in before + after if n == "commentary"]
            assert len(lines) == len(set(lines))
            await second.drop()

    asyncio.run(asyncio.wait_for(run(), 90))


def test_no_model_or_speech_calls_while_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 30)
    settings = _cached()
    pipeline = _CountingPipeline(PunditPipeline(settings, None))
    synthesizer = _CountingSynthesis()
    app, routes = _app(settings, pipeline, synthesizer)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.114"
            replay_id = await _start(routes, ip)
            session = app.state.sessions[replay_id]
            connection = _Connection(await _stream(routes, ip, replay_id))
            await connection.read(lambda got: sum(n == "commentary" for _, n, _ in got) >= 1)
            assert pipeline.calls > 0
            await connection.drop()
            assert session.replay.state == "paused"
            # Let a call already in flight when the pause landed finish. (A window whose line
            # is written stays pending, waiting for its event's release; it calls nothing.)
            calls = await _settled(lambda: (pipeline.calls, synthesizer.calls))
            sent = len(session.outbox)
            # At this clock 2 s is about 40 match minutes: a running replay would call often.
            await asyncio.sleep(2)
            assert (pipeline.calls, synthesizer.calls) == calls
            assert len(session.outbox) == sent
            # And it does call again once resumed.
            second = _Connection(await _stream(routes, ip, replay_id, str(sent)))
            await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "resume"}, ip, replay_id)
            )
            await _until(lambda: pipeline.calls > calls[0], limit=20)
            await _until(lambda: synthesizer.calls > calls[1], limit=20)
            await second.drop()

    asyncio.run(asyncio.wait_for(run(), 60))


# A full app takes a slot from a viewer who has gone


async def _start_as(routes, ip: str, client_id: str) -> str:
    started = await routes["/api/matches/{match_id}/start"](
        "m01", Request(_scope("/api/matches/m01/start", ip, client_id=client_id)), speed=1
    )
    _TOKENS[started["replay_id"]] = started["replay_token"]
    return started["replay_id"]


async def _fill_slots(routes, slots: int) -> list[tuple[str, str, Any]]:
    """Start `slots` replays, each a different viewer on its own address, each watching."""
    viewers = []
    for index in range(slots):
        ip = f"198.51.100.{150 + index}"
        replay_id = await _start_as(routes, ip, f"evict-viewer-{index}")
        connection = _Connection(await _stream(routes, ip, replay_id))
        await connection.read(lambda got: True, timeout=10)  # the first message
        viewers.append((ip, replay_id, connection))
    return viewers


def test_a_full_app_evicts_the_paused_replay_whose_viewer_has_gone() -> None:
    app, routes = _app(_cached(max_concurrent_replays=6))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            viewers = await _fill_slots(routes, 6)
            away_ip, away_id, away_connection = viewers[2]
            await away_connection.drop()  # the phone is locked: paused, no stream
            assert app.state.sessions[away_id].replay.state == "paused"
            replay_id = await _start_as(routes, "203.0.113.150", "evict-newcomer")
            assert replay_id in app.state.sessions
            assert away_id not in app.state.sessions
            assert len(app.state.sessions) == 6
            # The evicted viewer comes back: 404, and the page says the replay has ended.
            with pytest.raises(HTTPException) as gone:
                await _stream(routes, away_ip, away_id, "1")
            assert gone.value.status_code == 404
            # Everyone still watching keeps their replay.
            for ip, kept, connection in viewers:
                if kept != away_id:
                    assert kept in app.state.sessions
                    await connection.drop()

    asyncio.run(asyncio.wait_for(run(), 60))


def test_the_viewer_gone_longest_is_evicted_first() -> None:
    app, routes = _app(_cached(max_concurrent_replays=3))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            viewers = await _fill_slots(routes, 3)
            await viewers[1][2].drop()  # gone first
            await asyncio.sleep(0.05)
            await viewers[0][2].drop()  # gone second
            await _start_as(routes, "203.0.113.151", "evict-newcomer-2")
            assert viewers[1][1] not in app.state.sessions
            assert viewers[0][1] in app.state.sessions

    asyncio.run(asyncio.wait_for(run(), 60))


def test_with_every_viewer_watching_a_full_app_is_still_busy() -> None:
    app, routes = _app(_cached(max_concurrent_replays=3))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            viewers = await _fill_slots(routes, 3)
            with pytest.raises(HTTPException) as busy:
                await _start_as(routes, "203.0.113.152", "evict-newcomer-3")
            assert busy.value.status_code == 503
            assert busy.value.detail["code"] == "busy"
            assert all(replay_id in app.state.sessions for _, replay_id, _ in viewers)

    asyncio.run(asyncio.wait_for(run(), 60))


def test_at_the_address_cap_a_viewer_who_has_gone_gives_way() -> None:
    # Three phones on one home network; one is locked. A fourth viewer there can start.
    app, routes = _app(_cached(max_concurrent_replays=6))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.170"
            viewers = []
            for index in range(3):
                replay_id = await _start_as(routes, ip, f"home-viewer-{index}")
                connection = _Connection(await _stream(routes, ip, replay_id))
                await connection.read(lambda got: True, timeout=10)
                viewers.append((replay_id, connection))
            await viewers[0][1].drop()  # locked
            newcomer = await _start_as(routes, ip, "home-viewer-3")
            assert newcomer in app.state.sessions
            assert viewers[0][0] not in app.state.sessions
            # With all three present, the address cap still refuses a fourth.
            with pytest.raises(HTTPException) as capped:
                await _start_as(routes, ip, "home-viewer-4")
            assert capped.value.status_code == 429

    asyncio.run(asyncio.wait_for(run(), 60))
