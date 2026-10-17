"""Phase 4 group 3: the daily spending guard."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import replace
from datetime import date

import pytest
from fastapi import HTTPException, Request, UploadFile

from second_screen_pundit.api import QuestionRequest, create_app
from second_screen_pundit.engine.generate import generate_match
from second_screen_pundit.spending import CACHED_NOTICE, SpendingGuard
from tests.test_api import _GoalCutoffPipeline, _session, _settings, _wav
from tests.test_stream_resume import _TOKENS, _Connection, _faster_clock, _scope, _stream


class _NoModel:
    """A live pipeline that must not be used: any call is a model call the guard should stop."""

    async def warm_up(self) -> None:
        return None

    def __getattr__(self, name: str):
        raise AssertionError(f"the live pipeline was used ({name}) after the guard tripped")


class _CountingSynth:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize_mp3(self, text, path, *, tier=None):
        from types import SimpleNamespace

        self.texts.append(text)
        path.write_bytes(b"mp3")
        return SimpleNamespace(path=path)


def test_the_guard_counts_trips_once_and_resets_each_day(caplog) -> None:
    day = [date(2026, 10, 22)]
    guard = SpendingGuard(3, 100, today=lambda: day[0])
    assert not guard.tripped
    for _ in range(3):
        guard.record_model_call()
    assert guard.models_exhausted and guard.tripped
    assert guard.allow_speech(60) and not guard.allow_speech(41)  # 60 + 41 > 100
    with caplog.at_level(logging.WARNING, logger="second_screen_pundit.spending"):
        for _ in range(5):
            assert guard.tripped
    trips = [r.message for r in caplog.records if "spending_guard_tripped" in r.message]
    assert len(trips) <= 2  # at most one per cap per day, however often it's checked
    day[0] = date(2026, 10, 23)
    assert not guard.tripped
    assert guard.model_calls == 0 and guard.tts_characters == 0


def test_a_zero_cap_trips_at_once() -> None:
    assert SpendingGuard(0, 1000).tripped
    assert SpendingGuard(1000, 0).tripped
    with pytest.raises(ValueError):
        SpendingGuard(-1, 0)


def test_with_the_cap_reached_new_viewers_get_cached_mode_with_audio_off(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    _faster_clock(monkeypatch, 30)
    settings = replace(_settings(delay=0.2), mode="live", model_calls_per_day=0)
    synth = _CountingSynth()
    app = create_app(
        settings=settings,
        pipeline=_NoModel(),  # type: ignore[arg-type]
        credential=object(),
        speech_recognizer=object(),  # type: ignore[arg-type]
        speech_synthesizer=synth,  # type: ignore[arg-type]
    )
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.90"
            health = (await routes["/healthz"]()).body
            assert b'"limited":true' in health and b'"ready":true' in health
            with caplog.at_level(logging.WARNING):
                started = await routes["/api/matches/{match_id}/start"](
                    "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
                )
            assert started["mode"] == "cached"
            assert started["audio"] is False
            assert started["notice"] == CACHED_NOTICE
            assert any("spending_guard_tripped kind=model_calls" in r.message for r in caplog.records)
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            connection = _Connection(await _stream(routes, ip, replay_id))
            received = await connection.read(lambda got: got[-1][1] == "complete", timeout=60)
            await connection.drop()

            lines = [p for _, n, p in received if n == "commentary"]
            assert lines, "cached mode still delivers the pre-written lines"
            assert all(not p["audio_available"] and not p["audio_pending"] for p in lines)
            names = {n for _, n, _ in received}
            assert not names & {"commentary_audio", "commentary_audio_unavailable", "error"}
            recap = next(p for _, n, p in received if n == "recap")
            assert recap["audio_pending"] is False
            assert "recap_audio" not in names

            # A typed question is answered from the cache, with no audio to wait for.
            answered = await routes["/api/ask"](
                QuestionRequest(replay_id=replay_id, question="Who scored first?"),
                Request(_scope("/api/ask", ip, token=started["replay_token"])),
            )
            assert answered["source"] in {"cached", "fallback", "out_of_scope"}
            assert answered["audio_pending"] is False
            # Spoken questions are off for this replay, politely.
            with pytest.raises(HTTPException) as spoken:
                await routes["/api/ask-audio"](
                    Request(_scope("/api/ask-audio", ip, token=started["replay_token"])),
                    replay_id=replay_id,
                    audio=UploadFile(io.BytesIO(_wav()), filename="question.wav"),
                )
            assert spoken.value.status_code == 409
            assert "type your question" in spoken.value.detail

    asyncio.run(asyncio.wait_for(run(), 90))
    assert synth.texts == []  # no speech at all


def test_reaching_the_tts_cap_mid_replay_stops_speech_but_not_the_text() -> None:
    from fastapi.testclient import TestClient

    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    synth = _CountingSynth()
    settings = replace(_settings(), tts_characters_per_day=10)
    app = create_app(
        settings=settings,
        pipeline=_GoalCutoffPipeline(goal.seq),  # type: ignore[arg-type]
        speech_recognizer=object(),  # type: ignore[arg-type]
        speech_synthesizer=synth,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        session = _session(
            "tts-cap", goal_seq=goal.seq, released_as_of=goal.seq, client_ip="198.51.100.91"
        )
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Has a goal been scored?"},
            headers={"X-Forwarded-For": "198.51.100.91", "X-Replay-Token": session.token},
        )

        async def audio_outcome() -> str:
            async with asyncio.timeout(5):
                while True:
                    for _, name, _ in session.outbox.after(0):
                        if name.startswith("answer_audio"):
                            return name
                    await asyncio.sleep(0.02)

        outcome = client.portal.call(audio_outcome)
    assert response.status_code == 200
    assert response.json()["explanation"]["answer"]  # the text arrives
    assert outcome == "answer_audio_unavailable"  # the speech is refused, nothing fails
    assert synth.texts == []
