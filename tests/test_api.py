from __future__ import annotations

import asyncio
import io
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import Request
from fastapi.testclient import TestClient

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.content_filter import viewer_output_block_reason
from second_screen_pundit.agents.evidence import EvidenceReport
from second_screen_pundit.agents.models import Explanation
from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.api import (
    MAX_QUESTION_AUDIO_BYTES,
    MAX_QUESTION_AUDIO_SECONDS,
    QuestionRateLimiter,
    ReplaySession,
    _read_wav_duration,
    _evidence_payload,
    _safe_explanation,
    _safe_question_explanation,
    create_app,
)
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay


def _settings(*, delay: float = 0) -> AgentSettings:
    return AgentSettings(
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
        broadcast_delay_seconds=delay,
    )


class _GoalCutoffPipeline:
    def __init__(self, goal_seq: int) -> None:
        self.goal_seq = goal_seq
        self.received_as_of: int | None = None

    async def answer_question(self, _info, _events, _question, *, as_of: int):
        self.received_as_of = as_of
        answer = (
            f"The goal is visible at m02-e{self.goal_seq:04d}."
            if as_of >= self.goal_seq
            else "No goal is visible at the released position."
        )
        return SimpleNamespace(explanation=Explanation(answer=answer))


class _SpeechStub:
    def recognize_wav(self, _audio: bytes) -> str:
        return "Has a goal been scored?"


class _SynthesisStub:
    def synthesize_mp3(self, _text: str, path: Path, *, tier=None):
        path.write_bytes(b"stub-mp3")
        return SimpleNamespace(path=path)


def _session(
    replay_id: str,
    *,
    goal_seq: int,
    released_as_of: int,
) -> ReplaySession:
    events = generate_match("m02")
    replay = Replay(events, 20)
    replay.as_of = goal_seq
    return ReplaySession(
        replay_id=replay_id,
        tab_id="test-client:test-tab",
        client_ip="testclient",
        info=get_match_info("m02"),
        events=events,
        replay=replay,
        delay=BroadcastDelay(0),
        viewer_released_as_of=released_as_of,
    )


def _wav(seconds: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * (16000 * seconds))
    return output.getvalue()


def test_typed_question_uses_viewer_release_not_newer_agent_position() -> None:
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    pipeline = _GoalCutoffPipeline(goal.seq)
    app = create_app(settings=_settings(), pipeline=pipeline)  # type: ignore[arg-type]

    with TestClient(app) as client:
        session = _session("cutoff-replay", goal_seq=goal.seq, released_as_of=goal.seq - 1)
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Has a goal been scored?"},
            headers={"X-Forwarded-For": "198.51.100.22"},
        )

    assert response.status_code == 200
    assert pipeline.received_as_of == goal.seq - 1
    assert goal.event_id not in response.json()["explanation"]["answer"]
    assert "No goal is visible" in response.json()["explanation"]["answer"]


def test_spoken_question_uses_the_same_viewer_cutoff_and_returns_server_audio() -> None:
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    pipeline = _GoalCutoffPipeline(goal.seq)
    app = create_app(
        settings=_settings(),
        pipeline=pipeline,  # type: ignore[arg-type]
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=_SynthesisStub(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        session = _session("spoken-cutoff-replay", goal_seq=goal.seq, released_as_of=goal.seq - 1)
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask-audio",
            data={"replay_id": session.replay_id},
            files={"audio": ("question.wav", _wav(), "audio/wav")},
            headers={"X-Forwarded-For": "198.51.100.23"},
        )
        body = response.json()
        audio = client.get(f"/api/audio/{body['audio_id']}")

    assert response.status_code == 200
    assert body["transcript"] == "Has a goal been scored?"
    assert pipeline.received_as_of == goal.seq - 1
    assert goal.event_id not in body["explanation"]["answer"]
    assert audio.content == b"stub-mp3"


def test_viewer_output_filter_blocks_unknown_names_and_betting_talk() -> None:
    info = get_match_info("m01")

    assert viewer_output_block_reason(["North Coast League is in the table."], info)
    assert (
        viewer_output_block_reason(
            ["Merehaven Athletic had more chances after Half Time."],
            get_match_info("m02"),
        )
        is None
    )
    assert viewer_output_block_reason(["The odds are tempting."], info) == "betting language"
    assert viewer_output_block_reason(["Ashbridge Rovers control possession."], info) is None

    explanation, reason = _safe_explanation(
        Explanation(answer="The odds are tempting."),
        info,
    )
    assert reason == "betting language"
    assert "odds" not in explanation.answer.casefold()
    assert explanation.notice is not None


def test_evidence_report_is_serialized_for_sse_payloads() -> None:
    report = EvidenceReport(False, ("claim lacks a cited fact",))

    assert _evidence_payload(report) == {
        "passed": False,
        "failures": ["claim lacks a cited fact"],
    }
    assert _evidence_payload(None) is None


def test_blocked_question_returns_only_verified_fallback_facts() -> None:
    info = get_match_info("m02")
    events = generate_match("m02")
    as_of = events[-1].seq
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    result = asyncio.run(
        pipeline.answer_question(info, events, "What happened so far?", as_of=as_of)
    )
    unsafe_result = replace(
        result,
        explanation=Explanation(answer="North Coast League is dominating the match."),
    )

    explanation, reason = _safe_question_explanation(
        pipeline,
        unsafe_result,
        info,
        events,
        as_of,
    )

    assert reason == "an unapproved proper name"
    assert explanation.answer == "Live answers are unavailable; verified match facts follow."
    assert explanation.claims
    assert "North Coast League" not in str(explanation.model_dump())
    assert viewer_output_block_reason(
        [
            explanation.answer,
            *(claim.text for claim in explanation.claims),
            explanation.notice or "",
        ],
        info,
    ) is None


def test_interactive_timeouts_use_short_fallback_budgets_by_default(monkeypatch) -> None:
    for name in (
        "LIVE_WINDOW_TIMEOUT_SECONDS",
        "EXPLAINER_TIMEOUT_SECONDS",
        "SPEECH_SYNTHESIS_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AgentSettings.from_environment()

    assert settings.live_timeout_seconds == 8
    assert settings.explainer_timeout_seconds == 10
    assert settings.speech_synthesis_timeout_seconds == 8


def test_combined_question_rate_limit_has_clear_retry_after() -> None:
    limiter = QuestionRateLimiter()
    for request_number in range(6):
        limiter.check("client", now=float(request_number))

    try:
        limiter.check("client", now=6.0)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 429
        assert exc.headers["Retry-After"] == "54"
        assert "6 questions per minute" in exc.detail
    else:
        raise AssertionError("The seventh question was not rate limited")


def test_question_and_replay_limits_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QUESTIONS_PER_MINUTE", "9")
    monkeypatch.setenv("QUESTION_MAX_CHARACTERS", "240")
    monkeypatch.setenv("QUESTION_AUDIO_MAX_SECONDS", "12")
    monkeypatch.setenv("QUESTION_AUDIO_MAX_BYTES", "900000")
    monkeypatch.setenv("MAX_CONCURRENT_REPLAYS", "2")
    configured = AgentSettings.from_environment()

    assert configured.questions_per_minute == 9
    assert configured.question_max_characters == 240
    assert configured.question_audio_max_seconds == 12
    assert configured.question_audio_max_bytes == 900000
    assert configured.max_concurrent_replays == 2

    monkeypatch.setenv("QUESTIONS_PER_MINUTE", "0")
    try:
        AgentSettings.from_environment()
    except ValueError as exc:
        assert "QUESTIONS_PER_MINUTE" in str(exc)
    else:
        raise AssertionError("A zero question limit was accepted")


def test_wav_duration_enforces_the_audio_limit_and_rejects_invalid_data() -> None:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * (16000 * MAX_QUESTION_AUDIO_SECONDS))
    assert _read_wav_duration(output.getvalue()) == MAX_QUESTION_AUDIO_SECONDS
    assert len(output.getvalue()) <= MAX_QUESTION_AUDIO_BYTES

    too_long = io.BytesIO()
    with wave.open(too_long, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * (16000 * (MAX_QUESTION_AUDIO_SECONDS + 1)))
    try:
        _read_wav_duration(too_long.getvalue())
    except ValueError as exc:
        assert "15 seconds" in str(exc)
    else:
        raise AssertionError("An over-length recording was accepted")


def test_match_start_enforces_one_replay_per_tab_and_the_three_replay_cap() -> None:
    app = create_app(settings=_settings(), pipeline=PunditPipeline(_settings(), None))
    with TestClient(app) as client:
        headers = {"X-Tab-ID": "tab-one"}
        first = client.post("/api/matches/m02/start?speed=120", headers=headers)
        assert first.status_code == 200
        assert client.post("/api/matches/m01/start?speed=120", headers=headers).status_code == 409
        assert client.post(
            "/api/matches/m01/start?speed=120",
            headers={"X-Tab-ID": "tab-two"},
        ).status_code == 200
        assert client.post(
            "/api/matches/m03/start?speed=120",
            headers={"X-Tab-ID": "tab-three"},
        ).status_code == 200
        capped = client.post(
            "/api/matches/m01/start?speed=120",
            headers={"X-Tab-ID": "tab-four"},
        )

    assert capped.status_code == 503
    assert "3 replay slots" in capped.json()["detail"]


def test_sse_disconnect_stops_the_replay_and_removes_its_session() -> None:
    app = create_app(settings=_settings(), pipeline=PunditPipeline(_settings(), None))

    async def run_disconnect() -> tuple[ReplaySession, bool]:
        async with app.router.lifespan_context(app):
            events = generate_match("m02")
            replay = Replay(events, 20)
            session = ReplaySession(
                replay_id="disconnect-replay",
                tab_id="test-client:disconnect-tab",
                client_ip="testclient",
                info=get_match_info("m02"),
                events=events,
                replay=replay,
                delay=BroadcastDelay(0),
            )
            app.state.sessions[session.replay_id] = session
            app.state.tab_sessions[session.tab_id] = session.replay_id
            replay.start()
            worker = asyncio.create_task(asyncio.sleep(30))
            session.runner_task = worker
            session.queue.put_nowait(("event", {"event_id": "m02-e0001"}))
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/replays/{replay_id}/stream"
            )
            response = await route.endpoint(session.replay_id)
            chunk = await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
            return session, b"event: event" in chunk

    session, got_event = asyncio.run(run_disconnect())

    assert got_event
    assert session.stopped
    assert session.replay.state == "stopped"
    assert session.replay_id not in app.state.sessions


def test_sse_reports_worker_failure_and_completes_full_time(
    monkeypatch,
) -> None:
    events = generate_match("m02")
    kickoff = next(event for event in events if event.type == "kickoff")
    full_time = next(event for event in events if event.type == "full_time")
    short_events = [kickoff, full_time.model_copy(update={"minute": 0, "second": 0})]
    monkeypatch.setattr(
        "second_screen_pundit.api.generate_match",
        lambda _match_id: short_events,
    )

    async def fail_dispatcher(_self, _source, *, replay_stopped=None) -> None:
        raise RuntimeError("agent worker failed")

    monkeypatch.setattr("second_screen_pundit.api.LiveDispatcher.run", fail_dispatcher)
    settings = _settings()
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))

    async def consume_stream() -> str:
        async with app.router.lifespan_context(app):
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/matches/m02/start",
                "raw_path": b"/api/matches/m02/start",
                "query_string": b"",
                "headers": [(b"x-tab-id", b"worker-failure-tab")],
                "client": ("198.51.100.25", 1234),
                "server": ("127.0.0.1", 8000),
            }
            start_endpoint = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/matches/{match_id}/start"
            )
            stream_endpoint = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/api/replays/{replay_id}/stream"
            )
            started = await start_endpoint("m02", Request(scope), speed=120)
            session = app.state.sessions[started["replay_id"]]
            response = await stream_endpoint(started["replay_id"])
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if b"event: complete" in chunk:
                    break
            await response.body_iterator.aclose()
            assert session.complete
            return b"".join(chunks).decode("utf-8")

    stream = asyncio.run(consume_stream())
    assert "event: event" in stream
    assert "full_time" in stream
    assert "event: error" in stream
    assert "Replay worker failed (RuntimeError)" in stream
    assert "event: complete" in stream


def test_live_model_warmup_runs_in_background_and_gates_start() -> None:
    live_settings = AgentSettings(
        mode="live",
        endpoint="configured",
        model_fast="fast",
        model_deep="deep",
        speech_endpoint=None,
        speech_region=None,
        live_timeout_seconds=15,
        explainer_timeout_seconds=20,
        recap_timeout_seconds=45,
        importance_threshold=0.45,
        cooldown_match_minutes=4,
    )

    class WarmingPipeline:
        async def warm_up(self) -> None:
            await asyncio.sleep(0.1)

    app = create_app(
        settings=live_settings,
        pipeline=WarmingPipeline(),  # type: ignore[arg-type]
        credential=object(),
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json()["warmup"] == "warming"
        gated = client.post(
            "/api/matches/m02/start?speed=20",
            headers={"X-Tab-ID": "warmup-tab"},
        )
        assert gated.status_code == 503
        time.sleep(0.15)
        assert client.get("/healthz").json()["warmup"] == "ready"
