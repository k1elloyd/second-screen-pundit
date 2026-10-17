"""Phase 4: the viewer stream survives a dropped connection and the ingress request timeout.

Each test drops the stream (closes the response body, as a lost socket does) and reconnects
with Last-Event-ID, the way the page does. The match runs in cached mode, so the lines are
real pre-generated lines with real citations and no model is called.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException, Request, UploadFile

from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.api import QuestionRequest, create_app
from second_screen_pundit.engine.generate import generate_match
from second_screen_pundit.engine.replay import Replay
from tests.test_api import _settings, _SpeechStub, _SynthesisStub, _wav

Message = tuple[int, str, dict[str, Any]]


# replay_id -> replay token, recorded as each test starts a replay (the page keeps it in memory).
_TOKENS: dict[str, str] = {}


def _scope(
    path: str,
    ip: str,
    method: str = "POST",
    *,
    client_id: str = "resume-client",
    token: str | None = None,
) -> dict[str, Any]:
    headers = [(b"x-client-id", client_id.encode())]
    if token is not None:
        headers.append((b"x-replay-token", token.encode()))
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers,
        "client": (ip, 1234), "server": ("127.0.0.1", 8000),
    }


def _faster_clock(monkeypatch: pytest.MonkeyPatch, factor: float) -> None:
    """Run the match clock `factor` times faster than 120x; pause still holds it."""
    original = Replay._wait_interval

    async def faster(self: Replay, seconds: float) -> None:
        await original(self, seconds / factor)

    monkeypatch.setattr(Replay, "_wait_interval", faster)


async def _stream(
    routes, ip: str, replay_id: str, last_event_id: str | None = None, *, token: str | None = None
):
    token = _TOKENS.get(replay_id) if token is None else token
    request = Request(_scope(f"/api/replays/{replay_id}/stream", ip, "GET", token=token))
    return await routes["/api/replays/{replay_id}/stream"](replay_id, request, last_event_id)


class _Connection:
    """One stream connection, parsed the way the page parses it."""

    def __init__(self, response: Any) -> None:
        self.body = response.body_iterator
        self.buffer = ""
        self.retry: int | None = None
        self.ended = False

    async def read(self, until, *, timeout: float = 20) -> list[Message]:
        received: list[Message] = []

        async def pump() -> None:
            async for chunk in self.body:
                self.buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
                while "\n\n" in self.buffer:
                    block, self.buffer = self.buffer.split("\n\n", 1)
                    fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                    if "retry" in fields:
                        self.retry = int(fields["retry"])
                    if "event" in fields:
                        received.append(
                            (int(fields["id"]), fields["event"], json.loads(fields["data"]))
                        )
                        if until(received):
                            return
            self.ended = True

        await asyncio.wait_for(pump(), timeout)
        return received

    async def drop(self) -> None:
        await self.body.aclose()


def _app(settings, pipeline=None):
    settings = replace(_settings(delay=0.2), mode="cached") if settings is None else settings
    app = create_app(
        settings=settings,
        pipeline=pipeline or PunditPipeline(settings, None),
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=_SynthesisStub(),  # type: ignore[arg-type]
    )
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    return app, routes


def _assert_one_clean_stream(messages: list[Message], match_events: list) -> None:
    ids = [message_id for message_id, _, _ in messages]
    assert ids == list(range(1, len(ids) + 1)), "gap or duplicate in message IDs"
    seqs = [payload["seq"] for _, name, payload in messages if name == "event"]
    assert seqs == [event.seq for event in match_events], "events missing, repeated or reordered"
    line_ids = [payload["line"]["line_id"] for _, name, payload in messages if name == "commentary"]
    assert line_ids and len(line_ids) == len(set(line_ids)), "a line was sent twice"
    seen: set[str] = set()
    for _, name, payload in messages:
        if name == "event":
            seen.add(payload["event_id"])
        if name == "commentary":
            early = set(payload["line"]["event_ids"]) - seen
            assert not early, f"line {payload['line']['line_id']} arrived before {early}"


def test_reconnect_mid_match_resumes_with_no_duplicates_gaps_or_early_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(None)
    match_events = generate_match("m01")

    async def run() -> tuple[list[Message], list[Message], int, Any]:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.60"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            first = _Connection(await _stream(routes, ip, replay_id))
            before = await first.read(
                lambda got: sum(name == "commentary" for _, name, _ in got) >= 2
            )
            await first.drop()
            session = app.state.sessions[replay_id]
            sent_at_drop = before[-1][0]
            await asyncio.sleep(0.5)  # the match carries on while the page is away
            missed = len(session.outbox) - sent_at_drop
            second = _Connection(
                await _stream(routes, ip, 
                    replay_id, last_event_id=str(sent_at_drop)
                )
            )
            after = await second.read(lambda got: got[-1][1] == "complete", timeout=60)
            assert second.retry == first.retry == 1000
            assert list(app.state.sessions) == [replay_id]  # same replay, not restarted
            await second.drop()
            return before, after, missed, session

    before, after, missed, session = asyncio.run(run())
    assert missed > 0, "nothing was sent while disconnected, so resume wasn't exercised"
    assert any(event["type"] != "full_time" for _, name, event in after if name == "event")
    _assert_one_clean_stream(before + after, match_events)
    assert session.replay.state == "finished"


def test_a_spoken_answer_pending_across_a_drop_arrives_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 30)
    settings = replace(_settings(delay=0.2), mode="cached")
    gate = asyncio.Event()

    class _HeldPipeline(PunditPipeline):
        """Holds the answer until the stream has dropped."""

        async def answer_question(self, info, events, question, *, as_of):
            self.as_of = as_of
            await gate.wait()
            return await super().answer_question(info, events, question, as_of=as_of)

    pipeline = _HeldPipeline(settings, None)
    app, routes = _app(settings, pipeline)

    async def run() -> tuple[list[Message], dict, int, Any]:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.61"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            session = app.state.sessions[replay_id]
            first = _Connection(await _stream(routes, ip, replay_id))
            before = await first.read(lambda got: sum(n == "event" for _, n, _ in got) >= 20)
            await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "pause"}, ip, replay_id)
            )
            viewer_cutoff = session.viewer_released_as_of
            accepted = await routes["/api/ask-audio"](
                Request(_scope("/api/ask-audio", ip, token=_TOKENS[replay_id])),
                replay_id=replay_id,
                audio=UploadFile(io.BytesIO(_wav()), filename="question.wav"),
            )
            await first.drop()
            gate.set()  # the answer is checked and spoken while the page is away
            async with asyncio.timeout(10):
                while not any(name == "answer_audio" for _, name, _ in session.outbox.after(0)):
                    await asyncio.sleep(0.02)
            second = _Connection(
                await _stream(routes, ip, 
                    replay_id, last_event_id=str(before[-1][0])
                )
            )
            after = await second.read(lambda got: got[-1][1] == "answer_audio")
            await second.drop()
            assert pipeline.as_of == viewer_cutoff
            return before + after, accepted, viewer_cutoff, session

    messages, accepted, viewer_cutoff, session = asyncio.run(run())
    question_id = accepted["question_id"]
    answers = [p for _, n, p in messages if n == "answer" and p["question_id"] == question_id]
    audio = [p for _, n, p in messages if n == "answer_audio" and p["question_id"] == question_id]
    assert len(answers) == 1
    assert len(audio) == 1
    ids = [message_id for message_id, _, _ in messages]
    assert ids == list(range(1, len(ids) + 1))
    # The cut-off was the viewer's released position, before and after the drop.
    assert session.viewer_released_as_of == viewer_cutoff


def _json_request(body: dict, ip: str, replay_id: str, token: str | None = None) -> Request:
    payload = json.dumps(body).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    token = _TOKENS.get(replay_id) if token is None else token
    return Request(_scope("/control", ip, token=token), receive)


def test_a_paused_replay_stays_paused_across_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(None)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.62"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            session = app.state.sessions[replay_id]
            first = _Connection(await _stream(routes, ip, replay_id))
            before = await first.read(lambda got: sum(n == "event" for _, n, _ in got) >= 20)
            paused = await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "pause"}, ip, replay_id)
            )
            assert paused["state"] == "paused"
            await asyncio.sleep(0.4)  # let anything already released drain into the outbox
            sent = len(session.outbox)
            released = session.viewer_released_as_of
            await first.drop()
            await asyncio.sleep(0.5)
            second = _Connection(
                await _stream(routes, ip, 
                    replay_id, last_event_id=str(before[-1][0])
                )
            )
            # Only what was released before the pause; then nothing while paused.
            caught_up: list[Message] = []
            if sent > before[-1][0]:
                caught_up = await second.read(lambda got: got[-1][0] >= sent)
            with pytest.raises(TimeoutError):
                await second.read(lambda _got: True, timeout=0.6)
            assert session.replay.state == "paused"
            assert session.delay.paused
            assert session.viewer_released_as_of == released
            assert len(session.outbox) == sent
            resumed = await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "resume"}, ip, replay_id)
            )
            assert resumed["state"] == "running"
            after = await second.read(lambda got: any(n == "event" for _, n, _ in got))
            ids = [i for i, _, _ in before + caught_up + after]
            assert ids == list(range(1, len(ids) + 1))
            await second.drop()

    asyncio.run(run())


def test_each_stream_closes_itself_before_the_ingress_timeout_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 4)
    settings = replace(_settings(delay=0.2), mode="cached", stream_max_seconds=0.5)
    app, routes = _app(settings)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.63"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            received: list[Message] = []
            closes = 0
            for _ in range(3):
                last = str(received[-1][0]) if received else None
                connection = _Connection(
                    await _stream(routes, ip, replay_id, last_event_id=last)
                )
                received += await connection.read(lambda _got: False, timeout=5)
                assert connection.ended  # the server closed it, cleanly, on its own
                closes += 1
            assert not app.state.sessions[replay_id].stopped
            ids = [i for i, _, _ in received]
            assert closes == 3 and len(ids) > 3
            assert ids == list(range(1, len(ids) + 1))

    asyncio.run(run())


def test_a_new_connection_replaces_an_unnoticed_dead_one() -> None:
    app, routes = _app(None)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.64"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=1
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            stale = (await _stream(routes, ip, replay_id)).body_iterator
            assert await stale.__anext__() == b"retry: 1000\n\n"
            # The stale stream is now waiting for a message (up to 15 s for a heartbeat).
            waiting = asyncio.create_task(stale.__anext__())
            await asyncio.sleep(0.05)
            fresh = await _stream(routes, ip, replay_id, last_event_id="0")
            # It ends at once instead of holding the replay.
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(waiting, 1)
            session = app.state.sessions[replay_id]
            assert session.stream_open and not session.stopped
            await fresh.body_iterator.aclose()

    asyncio.run(asyncio.wait_for(run(), 10))


# ---- A replay belongs to whoever holds its token, not to an address ----


def test_a_request_without_the_replay_token_is_refused() -> None:
    app, routes = _app(None)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.65"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=1
            )
            replay_id, token = started["replay_id"], started["replay_token"]
            # At least 128 bits: 24 random bytes, URL-safe base64.
            assert len(token) >= 32
            for bad in (None, "", "not-the-token", token[:-1] + ("A" if token[-1] != "A" else "B")):
                bad_token = "" if bad is None else bad
                for attempt in (
                    lambda: _stream(routes, ip, replay_id, token=bad_token),
                    lambda: _stream(routes, ip, replay_id, "5", token=bad_token),
                    lambda: routes["/api/replays/{replay_id}/control"](
                        replay_id, _json_request({"action": "pause"}, ip, replay_id, bad_token)
                    ),
                    lambda: routes["/api/ask"](
                        QuestionRequest(replay_id=replay_id, question="Who scored?"),
                        Request(_scope("/api/ask", ip, token=bad_token)),
                    ),
                    lambda: routes["/api/ask-audio"](
                        Request(_scope("/api/ask-audio", ip, token=bad_token)),
                        replay_id=replay_id,
                        audio=UploadFile(io.BytesIO(_wav()), filename="question.wav"),
                    ),
                ):
                    with pytest.raises(HTTPException) as refused:
                        await attempt()
                    assert refused.value.status_code == 404  # same as a missing replay
            session = app.state.sessions[replay_id]
            assert session.replay.state == "running" and not session.stream_open
            connected = await _stream(routes, ip, replay_id, token=token)
            assert session.stream_open
            await connected.body_iterator.aclose()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_two_viewers_on_one_address_each_run_a_replay() -> None:
    from fastapi.testclient import TestClient

    settings = replace(_settings(), max_concurrent_replays=5)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))
    office = "203.0.113.50"

    def start(client_id: str, ip: str = office):
        return client.post(
            "/api/matches/m01/start?speed=1",
            headers={"X-Client-ID": client_id, "X-Forwarded-For": ip},
        )

    with TestClient(app) as client:
        first, second = start("judge-a"), start("judge-b")
        assert first.status_code == second.status_code == 200
        alive = app.state.sessions
        assert first.json()["replay_id"] in alive and second.json()["replay_id"] in alive
        assert first.json()["replay_token"] != second.json()["replay_token"]
        # A viewer's new Start replaces only their own replay.
        again = start("judge-a")
        assert again.status_code == 200
        assert first.json()["replay_id"] not in alive and second.json()["replay_id"] in alive
        # Up to 3 running replays per address, within the global cap.
        assert start("judge-c").status_code == 200
        capped = start("judge-d")
        assert capped.status_code == 429
        assert "3 replays" in capped.json()["detail"]
        assert start("elsewhere", "198.51.100.66").status_code == 200


def test_a_reconnect_from_a_new_address_with_the_token_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 30)
    app, routes = _app(None)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            wifi, mobile = "198.51.100.67", "203.0.113.67"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", wifi)), speed=120
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            first = _Connection(await _stream(routes, wifi, replay_id))
            before = await first.read(lambda got: sum(n == "event" for _, n, _ in got) >= 30)
            await first.drop()
            await asyncio.sleep(0.3)
            # The phone is now on another network: a different address, the same token.
            second = _Connection(await _stream(routes, mobile, replay_id, str(before[-1][0])))
            after = await second.read(lambda got: sum(n == "event" for _, n, _ in got) >= 30)
            await second.drop()
            ids = [i for i, _, _ in before + after]
            assert ids == list(range(1, len(ids) + 1))
            assert list(app.state.sessions) == [replay_id]

    asyncio.run(asyncio.wait_for(run(), 30))
