"""Phase 3 Session B, group 3: replay slots, client IP and a free event loop."""

from __future__ import annotations

import asyncio
import gc
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.pipeline import LiveWindowResult, PunditPipeline
from second_screen_pundit.api import QuestionRateLimiter, _client_ip, create_app
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event
from tests.test_api import _settings


def _request(forwarded: str | None, peer: str = "10.0.0.5") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def _start(client: TestClient, ip: str, *, tab: str = "tab", match: str = "m02"):
    return client.post(
        f"/api/matches/{match}/start?speed=120",
        headers={"X-Client-ID": tab, "X-Forwarded-For": ip},
    )


# Client IP


def test_client_ip_is_the_right_most_forwarded_entry() -> None:
    # Container Apps ingress appends the real caller; anything to its left is client-chosen.
    assert _client_ip(_request("203.0.113.9, 198.51.100.7")) == "198.51.100.7"
    assert _client_ip(_request("1.1.1.1,2.2.2.2 , 198.51.100.7 ")) == "198.51.100.7"
    assert _client_ip(_request("198.51.100.7")) == "198.51.100.7"
    assert _client_ip(_request(None)) == "10.0.0.5"


def test_a_rotating_spoofed_prefix_does_not_bypass_the_question_limit() -> None:
    limiter = QuestionRateLimiter(limit=6, window_seconds=60)
    for index in range(6):
        limiter.check(_client_ip(_request(f"203.0.113.{index}, 198.51.100.51")), now=0)
    with pytest.raises(HTTPException) as blocked:
        limiter.check(_client_ip(_request("203.0.113.99, 198.51.100.51")), now=1)
    assert blocked.value.status_code == 429


def test_the_per_address_cap_holds_whatever_the_spoofed_prefix() -> None:
    # A replay belongs to a viewer (X-Client-ID); the address only caps running replays at 3.
    # Rotating a spoofed X-Forwarded-For prefix and the client ID doesn't escape that cap.
    settings = replace(_settings(), max_concurrent_replays=5)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))
    with TestClient(app) as client:
        responses = [
            _start(client, f"203.0.113.{index}, 198.51.100.60", tab=f"viewer-{index}")
            for index in range(4)
        ]
    assert [response.status_code for response in responses] == [200, 200, 200, 429]


# Replay slots expire without a stream


def test_a_replay_with_no_stream_frees_its_slot_after_the_deadline() -> None:
    settings = replace(_settings(), stream_connect_deadline_seconds=1.5, max_concurrent_replays=3)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))
    with TestClient(app) as client:
        for index in range(3):
            assert _start(client, f"198.51.100.{70 + index}", tab=f"v{index}").status_code == 200
        assert _start(client, "198.51.100.80", tab="late").status_code == 503
        # Starts take about 130 ms each (match generation), hence the 1.5 s deadline; allow
        # for a loaded test machine before calling it a failure.
        waited = time.monotonic() + 8
        while app.state.sessions and time.monotonic() < waited:
            time.sleep(0.05)
        assert app.state.sessions == {}
        assert _start(client, "198.51.100.80", tab="late").status_code == 200


def test_a_connected_stream_cancels_the_deadline() -> None:
    settings = replace(_settings(), stream_connect_deadline_seconds=0.3)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))

    async def run() -> bool:
        async with app.router.lifespan_context(app):
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/api/matches/m02/start",
                "raw_path": b"/api/matches/m02/start", "query_string": b"",
                "headers": [(b"x-client-id", b"deadline-tab")],
                "client": ("198.51.100.90", 1234), "server": ("127.0.0.1", 8000),
            }
            routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
            started = await routes["/api/matches/{match_id}/start"]("m02", Request(scope), speed=1)
            response = await routes["/api/replays/{replay_id}/stream"](
                started["replay_id"],
                Request({**scope, "headers": [(b"x-replay-token", started["replay_token"].encode())]}),
            )
            await response.body_iterator.__anext__()
            await asyncio.sleep(0.8)
            alive = started["replay_id"] in app.state.sessions
            await response.body_iterator.aclose()
            return alive

    assert asyncio.run(run())


# The event loop isn't blocked by the tactical check


class _SilentPipeline:
    async def comment_on_window(self, *_args, **_kwargs) -> LiveWindowResult:
        return LiveWindowResult("skipped", None, None, (), None)


async def _source(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        await asyncio.sleep(0)  # a replay subscriber's queue.get() yields to the loop
        yield event


async def _max_loop_gap(work) -> float:
    """Run work while a 5 ms ticker records the longest gap between its ticks."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        await work
    finally:
        done.set()
        await tick
    return max(gaps)


def _median_loop_gap(make_work, runs: int) -> tuple[float, list[float], list[str]]:
    """The median, over `runs` fresh runs, of each run's longest event-loop gap.

    One run's longest gap is a single sample, and in the full suite the test process is
    sometimes paused for a few hundred ms by something outside the code under test (one
    m02 run measured 266 ms there, one m01 run 398 ms; alone both stay under 100 ms). A
    blocking call in the dispatcher blocks every run, so the median still catches it; a
    one-off pause lands in one run and doesn't move the median. The threshold stays the
    same in and out of the suite. Garbage is collected before each run, and any full
    (generation 2) collection during a run is reported with its length, to show whether
    that is where a slow run's time went.
    """
    gaps: list[float] = []
    notes: list[str] = []
    collections: list[float] = []
    started: list[float] = []

    def on_gc(phase: str, info: dict) -> None:
        if info["generation"] != 2:
            return
        if phase == "start":
            started.append(time.perf_counter())
        elif started:
            collections.append(time.perf_counter() - started.pop())

    gc.callbacks.append(on_gc)
    try:
        for _ in range(runs):
            gc.collect()
            collections.clear()
            gaps.append(asyncio.run(_max_loop_gap(make_work())))
            if collections:
                notes.append(
                    f"run {len(gaps)}: {len(collections)} full GC, longest "
                    f"{max(collections) * 1000:.0f} ms"
                )
    finally:
        gc.callbacks.remove(on_gc)
    return statistics.median(gaps), gaps, notes


def _describe(gaps: list[float], notes: list[str]) -> str:
    runs = ", ".join(f"{gap * 1000:.0f}" for gap in gaps)
    return f"longest gap per run: {runs} ms" + (f" ({'; '.join(notes)})" if notes else "")


def test_a_slow_tactical_check_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = generate_match("m01")
    first_minutes = [event for event in events if event.minute < 7]
    dispatchers: list[LiveDispatcher] = []

    def slow_evaluate(_events, *, as_of):
        time.sleep(0.25)  # a boundary as slow as review 2's worst case (0.4 s)
        return []

    def work():
        dispatcher = LiveDispatcher(
            _SilentPipeline(), get_match_info("m01"), events, _settings()  # type: ignore[arg-type]
        )
        monkeypatch.setattr(dispatcher._tracker, "evaluate", slow_evaluate)
        dispatchers.append(dispatcher)
        return dispatcher.run(_source(first_minutes))

    gap, gaps, notes = _median_loop_gap(work, runs=5)
    assert all(len(dispatcher.boundaries) >= 2 for dispatcher in dispatchers)
    with capsys.disabled():
        print(f"\nslow tactical check, m01 to 7': {_describe(gaps, notes)}")
    # Each boundary sleeps 0.25 s in a worker thread; the loop keeps ticking meanwhile.
    assert gap < 0.1, f"event loop blocked for {gap * 1000:.0f} ms (median); {_describe(gaps, notes)}"


def test_a_full_match_of_tactical_checks_leaves_the_loop_responsive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = generate_match("m02")
    dispatchers: list[LiveDispatcher] = []

    def work():
        dispatcher = LiveDispatcher(
            _SilentPipeline(), get_match_info("m02"), events, _settings()  # type: ignore[arg-type]
        )
        dispatchers.append(dispatcher)
        return dispatcher.run(_source(events))

    gap, gaps, notes = _median_loop_gap(work, runs=3)
    assert all(len(dispatcher.boundaries) == 30 for dispatcher in dispatchers)
    with capsys.disabled():
        print(f"\nm02's 30 boundaries: {_describe(gaps, notes)}")
    assert gap < 0.2, f"event loop blocked for {gap * 1000:.0f} ms (median); {_describe(gaps, notes)}"


# The colour-line worker builds its candidate off the event loop


def test_colour_lines_are_built_without_blocking_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real colour worker, through the app: a slow candidate build must not stall the loop.

    A build is about a second of CPU over the match so far (review 3). Here each one also
    sleeps 0.25 s, as the slow tactical check above does, so a build run on the loop again
    shows as a gap of at least that. A colour line must still reach the viewer.
    """
    from second_screen_pundit import api
    from second_screen_pundit.agents import colour
    from tests.test_stream_resume import _TOKENS, _Connection, _json_request, _scope, _stream

    # Colour lines due at once and often; the match clock otherwise as at 120x.
    monkeypatch.setattr(colour, "COLOUR_FIRST_MINUTE", 0)
    monkeypatch.setattr(colour, "COLOUR_IDLE_REAL_SECONDS", 0.2)
    monkeypatch.setattr(colour, "COLOUR_SPACING_REAL_SECONDS", 1.0)
    monkeypatch.setattr(api, "COLOUR_RETRY_SECONDS", 0.3)
    real_build = api.build_colour_candidate
    builds: list[float] = []

    def slow_build(*args, **kwargs):
        builds.append(time.perf_counter())
        time.sleep(0.25)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api, "build_colour_candidate", slow_build)
    published: list[int] = []

    def work():
        # The spending cap at 0: cached lines, audio off, so the viewer's queue is never busy.
        settings = replace(_settings(delay=0.2), model_calls_per_day=0)
        app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))
        routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}

        async def run() -> None:
            async with app.router.lifespan_context(app):
                ip = "198.51.100.150"
                started = await routes["/api/matches/{match_id}/start"](
                    "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
                )
                replay_id = started["replay_id"]
                _TOKENS[replay_id] = started["replay_token"]
                connection = _Connection(await _stream(routes, ip, replay_id))
                await connection.read(lambda got: any(n == "event" for _, n, _ in got))
                await routes["/api/replays/{replay_id}/playback-state"](
                    replay_id,
                    api.ViewerPlaybackStateRequest(audio_busy=False, revision=1),
                    _json_request({}, ip, replay_id),
                )
                colour_lines = await connection.read(
                    lambda got: any(
                        n == "commentary" and p["line"]["source"] == "colour" for _, n, p in got
                    ),
                    timeout=30,
                )
                published.append(
                    sum(
                        n == "commentary" and p["line"]["source"] == "colour"
                        for _, n, p in colour_lines
                    )
                )
                await connection.drop()

        return run()

    gap, gaps, notes = _median_loop_gap(work, runs=3)
    with capsys.disabled():
        print(f"\ncolour-line builds ({len(builds)} in 3 runs): {_describe(gaps, notes)}")
    assert len(published) == 3 and all(count >= 1 for count in published)
    assert gap < 0.1, f"event loop blocked for {gap * 1000:.0f} ms (median); {_describe(gaps, notes)}"
