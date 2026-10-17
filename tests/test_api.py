from __future__ import annotations

import asyncio
import hashlib
import io
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from second_screen_pundit.agents import content_filter
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
    client_ip: str = "testclient",
) -> ReplaySession:
    events = generate_match("m02")
    replay = Replay(events, 20)
    replay.as_of = goal_seq
    return ReplaySession(
        replay_id=replay_id,
        client_id="test-client",
        client_ip=client_ip,
        info=get_match_info("m02"),
        events=events,
        replay=replay,
        delay=BroadcastDelay(0),
        viewer_released_as_of=released_as_of,
    )


def _get_request(client_ip: str, path: str = "/", *, token: str | None = None) -> Request:
    """A bare request from `client_ip`, for calling a route's endpoint directly."""
    headers = [] if token is None else [(b"x-replay-token", token.encode())]
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers, "client": (client_ip, 1234),
        "server": ("127.0.0.1", 8000),
    })


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
        session = _session(
            "cutoff-replay",
            goal_seq=goal.seq,
            released_as_of=goal.seq - 1,
            client_ip="198.51.100.22",
        )
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Has a goal been scored?"},
            headers={"X-Forwarded-For": "198.51.100.22", "X-Replay-Token": session.token},
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
        session = _session(
            "spoken-cutoff-replay",
            goal_seq=goal.seq,
            released_as_of=goal.seq - 1,
            client_ip="198.51.100.23",
        )
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask-audio",
            data={"replay_id": session.replay_id},
            files={"audio": ("question.wav", _wav(), "audio/wav")},
            headers={"X-Forwarded-For": "198.51.100.23", "X-Replay-Token": session.token},
        )
        body = response.json()

        read = 0

        async def next_item() -> tuple[str, dict]:
            nonlocal read
            ((read, name, payload), *_) = await session.outbox.read(read, timeout=5)
            return name, payload

        answer_name, answer = client.portal.call(next_item)
        audio_name, audio_event = client.portal.call(next_item)
        audio = client.get(f"/api/audio/{audio_event['audio_id']}")

    # The transcript comes back as soon as speech-to-text finishes; the answer follows on SSE.
    assert response.status_code == 202
    assert body["transcript"] == "Has a goal been scored?"
    assert body["status"] == "answering"
    assert "explanation" not in body
    assert answer_name == "answer"
    assert answer["question_id"] == body["question_id"]
    assert answer["transcript"] == "Has a goal been scored?"
    assert pipeline.received_as_of == goal.seq - 1
    assert goal.event_id not in answer["explanation"]["answer"]
    assert audio_name == "answer_audio"
    assert audio_event["question_id"] == body["question_id"]
    assert audio.content == b"stub-mp3"


def test_typed_answer_text_returns_at_once_and_its_audio_follows_on_sse() -> None:
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    pipeline = _GoalCutoffPipeline(goal.seq)
    synthesizer = _SynthesisStub()
    app = create_app(
        settings=_settings(),
        pipeline=pipeline,  # type: ignore[arg-type]
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=synthesizer,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        session = _session(
            "typed-audio-replay",
            goal_seq=goal.seq,
            released_as_of=goal.seq,
            client_ip="198.51.100.24",
        )
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Has a goal been scored?"},
            headers={"X-Forwarded-For": "198.51.100.24", "X-Replay-Token": session.token},
        )
        body = response.json()

        read = 0

        async def next_item() -> tuple[str, dict]:
            nonlocal read
            ((read, name, payload), *_) = await session.outbox.read(read, timeout=5)
            return name, payload

        audio_name, audio_event = client.portal.call(next_item)
        audio = client.get(f"/api/audio/{audio_event['audio_id']}")

    assert response.status_code == 200
    assert "goal is visible" in body["explanation"]["answer"]
    assert body["audio_pending"] is True
    assert body["audio_id"] is None
    assert audio_name == "answer_audio"
    assert audio_event["question_id"] == body["question_id"]
    assert audio.content == b"stub-mp3"


def _denylist(monkeypatch, name: str) -> None:
    """Add a fictional name to the hashed denylist, so no real name appears in the tests."""
    fingerprints = {
        *content_filter._KNOWN_REAL_NAME_FINGERPRINTS,
        hashlib.sha256(name.encode("utf-8")).hexdigest(),
    }
    monkeypatch.setattr(content_filter, "_KNOWN_REAL_NAME_FINGERPRINTS", fingerprints)


def test_viewer_output_filter_blocks_denylisted_names_and_betting_talk(monkeypatch) -> None:
    info = get_match_info("m01")
    _denylist(monkeypatch, "gravel town")

    assert viewer_output_block_reason(["Gravel Town are top of the table."], info) == (
        "a blocked real-club name"
    )
    assert viewer_output_block_reason(["Gravel Town's pressing is better."], info) == (
        "a blocked real-club name"
    )
    assert viewer_output_block_reason(["The odds are tempting."], info) == "betting language"
    assert viewer_output_block_reason(["Have a bet on the next goal."], info) == "betting language"
    assert viewer_output_block_reason(["Ashbridge Rovers control possession."], info) is None

    explanation, reason = _safe_explanation(
        Explanation(answer="The odds are tempting."),
        info,
    )
    assert reason == "betting language"
    assert "odds" not in explanation.answer.casefold()
    assert explanation.notice is not None


@pytest.mark.parametrize(
    "text",
    [
        # Review 2, finding 2: each of these was blocked before.
        "Port Calder's shape changed after the red card.",
        "Merehaven Athletic's pressing has been relentless.",
        "Merehaven have generated more xG since the red card.",
        "Merehaven have the better chances, with 1.42 xG.",
        "It is the Expected Goals that tell the story.",
        "The stakes are high for Port Calder.",
        "Port Calder are holding on against the odds.",
        "Merehaven Athletic have won against all odds before.",
        "Port Calder FC have kept a 4-4-1 since the red card at Saltmere Park.",
        "VAR has not been needed; Merehaven have camped in the final third.",
        # The notice that blocked itself (an exception class name looked like a proper name).
        "The live explanation failed (ChatClientException); verified facts are shown instead.",
    ],
)
def test_viewer_output_filter_allows_ordinary_football_english(text: str) -> None:
    assert viewer_output_block_reason([text], get_match_info("m02")) is None


def test_system_notices_and_fallback_facts_are_never_filtered(monkeypatch) -> None:
    info = get_match_info("m02")
    _denylist(monkeypatch, "verified")
    fallback = SimpleNamespace(
        source="fallback",
        explanation=Explanation(
            answer="Live answers are unavailable; verified match facts follow.",
            notice="The answer could not be verified; verified match facts are shown instead.",
        ),
    )

    explanation, reason = _safe_question_explanation(
        None,  # type: ignore[arg-type]
        fallback,  # type: ignore[arg-type]
        info,
        [],
        0,
    )

    assert reason is None
    assert explanation is fallback.explanation


def test_betting_and_real_name_questions_get_a_fixed_reply_without_a_model_call(
    monkeypatch,
) -> None:
    info = get_match_info("m02")
    events = generate_match("m02")
    _denylist(monkeypatch, "gravel town")
    pipeline = PunditPipeline(replace(_settings(), mode="live"), object())  # type: ignore[arg-type]

    def no_model(*_args, **_kwargs):
        raise AssertionError("an out-of-scope question must not call the model")

    monkeypatch.setattr(pipeline, "_agent", no_model)
    for question in (
        "What are the odds Merehaven win this?",
        "Should I bet on Port Calder?",
        "Is Merehaven's pressing better than Gravel Town's?",
    ):
        result = asyncio.run(
            pipeline.answer_question(info, events, question, as_of=events[-1].seq)
        )
        assert result.source == "out_of_scope", question
        assert result.attempts == 0
        assert "no betting" in result.explanation.answer


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
        source="agent",
        explanation=Explanation(answer="Have a bet on Merehaven, they are dominating."),
    )

    explanation, reason = _safe_question_explanation(
        pipeline,
        unsafe_result,
        info,
        events,
        as_of,
    )

    assert reason == "betting language"
    assert explanation.answer == "Live answers are unavailable; verified match facts follow."
    assert explanation.claims
    assert "bet" not in explanation.answer
    assert viewer_output_block_reason(
        [
            explanation.answer,
            *(claim.text for claim in explanation.claims),
            explanation.notice or "",
        ],
        info,
    ) is None


def test_interactive_timeouts_use_spec_budgets_by_default(monkeypatch) -> None:
    for name in (
        "LIVE_WINDOW_TIMEOUT_SECONDS",
        "EXPLAINER_TIMEOUT_SECONDS",
        "SPEECH_SYNTHESIS_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AgentSettings.from_environment()

    assert settings.live_timeout_seconds == 15
    assert settings.explainer_timeout_seconds == 20
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


def test_match_start_gives_each_client_one_replay_and_caps_at_three() -> None:
    # Session B: one replay per client IP (a new start replaces the old one) instead of one
    # per client-chosen tab ID, which never limited anything. The cap of 3 is unchanged.
    app = create_app(settings=_settings(), pipeline=PunditPipeline(_settings(), None))
    with TestClient(app) as client:
        def start(match: str, ip: str, tab: str):
            return client.post(
                f"/api/matches/{match}/start?speed=120",
                headers={"X-Client-ID": tab, "X-Forwarded-For": ip},
            )

        first = start("m02", "198.51.100.1", "tab-one")
        assert first.status_code == 200
        again = start("m01", "198.51.100.1", "tab-one")
        assert again.status_code == 200
        assert first.json()["replay_id"] not in app.state.sessions
        assert start("m01", "198.51.100.2", "tab-two").status_code == 200
        assert start("m03", "198.51.100.3", "tab-three").status_code == 200
        capped = start("m01", "198.51.100.4", "tab-four")

    assert capped.status_code == 503
    assert "3 replay slots" in capped.json()["detail"]


def test_sse_disconnect_stops_the_replay_after_the_reconnect_grace() -> None:
    # Phase 4: a dropped stream no longer stops the replay at once; the browser has
    # STREAM_RECONNECT_GRACE_SECONDS to reconnect before the replay and its session go.
    settings = replace(_settings(), stream_reconnect_grace_seconds=0.2)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))

    async def run_disconnect() -> tuple[ReplaySession, bool]:
        async with app.router.lifespan_context(app):
            events = generate_match("m02")
            replay = Replay(events, 20)
            session = ReplaySession(
                replay_id="disconnect-replay",
                client_id="disconnect-client",
                client_ip="testclient",
                info=get_match_info("m02"),
                events=events,
                replay=replay,
                delay=BroadcastDelay(0),
            )
            app.state.sessions[session.replay_id] = session
            app.state.client_sessions[session.client_id] = session.replay_id
            replay.start()
            worker = asyncio.create_task(asyncio.sleep(30))
            session.runner_task = worker
            session.outbox.put_nowait(("event", {"event_id": "m02-e0001"}))
            route = next(
                route
                for route in app.routes
                if getattr(route, "path", None) == "/api/replays/{replay_id}/stream"
            )
            response = await route.endpoint(
                session.replay_id, _get_request("testclient", token=session.token)
            )
            chunks = [await response.body_iterator.__anext__() for _ in range(2)]
            await response.body_iterator.aclose()
            assert not session.stopped  # still inside the grace period
            await asyncio.sleep(0.4)
            return session, b"event: event" in b"".join(chunks)

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
                "headers": [(b"x-client-id", b"worker-failure-tab")],
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
            response = await stream_endpoint(
                started["replay_id"], _get_request("198.51.100.25", token=started["replay_token"])
            )
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
    assert "The replay stopped unexpectedly" in stream
    assert "RuntimeError" not in stream
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
            headers={"X-Client-ID": "warmup-tab"},
        )
        assert gated.status_code == 503
        time.sleep(0.15)
        assert client.get("/healthz").json()["warmup"] == "ready"
