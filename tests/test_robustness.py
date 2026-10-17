"""Phase 3 Session B, group 3: replay slots, client IP and a free event loop."""

from __future__ import annotations

import asyncio
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


def test_a_slow_tactical_check_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m01")
    dispatcher = LiveDispatcher(
        _SilentPipeline(), get_match_info("m01"), events, _settings()  # type: ignore[arg-type]
    )

    def slow_evaluate(_events, *, as_of):
        time.sleep(0.25)  # a boundary as slow as review 2's worst case (0.4 s)
        return []

    monkeypatch.setattr(dispatcher._tracker, "evaluate", slow_evaluate)
    first_minutes = [event for event in events if event.minute < 7]

    gap = asyncio.run(_max_loop_gap(dispatcher.run(_source(first_minutes))))
    assert len(dispatcher.boundaries) >= 2
    # Each boundary sleeps 0.25 s in a worker thread; the loop keeps ticking meanwhile.
    assert gap < 0.1, f"event loop blocked for {gap * 1000:.0f} ms"


def test_a_full_match_of_tactical_checks_leaves_the_loop_responsive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = generate_match("m02")
    dispatcher = LiveDispatcher(
        _SilentPipeline(), get_match_info("m02"), events, _settings()  # type: ignore[arg-type]
    )
    gap = asyncio.run(_max_loop_gap(dispatcher.run(_source(events))))
    assert len(dispatcher.boundaries) == 30
    with capsys.disabled():
        print(f"\nlongest event-loop gap over m02's 30 boundaries: {gap * 1000:.0f} ms")
    assert gap < 0.2, f"event loop blocked for {gap * 1000:.0f} ms"
