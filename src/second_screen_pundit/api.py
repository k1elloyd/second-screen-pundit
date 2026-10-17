from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import secrets
import time
import wave
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.content_filter import viewer_output_block_reason
from second_screen_pundit.agents.dispatcher import LiveDispatcher, WindowOutcome
from second_screen_pundit.agents.evidence import EvidenceReport
from second_screen_pundit.agents.models import DeliveryTier, Explanation
from second_screen_pundit.agents.pipeline import PunditPipeline, QuestionResult
from second_screen_pundit.agents.speech import SpeechRecognizer, SpeechSynthesizer
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay, ReplayManager
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name
from second_screen_pundit.observability import record_result, tracer, turn_span
from second_screen_pundit.outbox import ViewerOutbox
from second_screen_pundit.spending import CACHED_NOTICE, SpendingGuard


logger = logging.getLogger(__name__)
MAX_QUESTION_AUDIO_BYTES = 1_000_000
MAX_QUESTION_AUDIO_SECONDS = 15
# Replays one network address may run at once, within the global cap: judges in one office
# share an address.
MAX_REPLAYS_PER_ADDRESS = 3
_WEB_DIR = Path(__file__).with_name("web")
SSE_HEARTBEAT_SECONDS = 15
# How long the browser waits before reconnecting a stream that closed or dropped.
SSE_RETRY_MILLISECONDS = 1000


class QuestionRequest(BaseModel):
    replay_id: str
    question: str


def _new_replay_token() -> str:
    return secrets.token_urlsafe(24)  # 192 bits


@dataclass
class ReplaySession:
    replay_id: str
    # The page's own ID (sessionStorage): one replay per viewer, whatever their address.
    client_id: str
    # The address is for rate limits and the per-address cap only, never for access.
    client_ip: str
    info: MatchInfo
    events: list[Event]
    replay: Replay
    delay: BroadcastDelay
    outbox: ViewerOutbox = field(default_factory=ViewerOutbox)
    release_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    released_events: list[Event] = field(default_factory=list)
    viewer_released_as_of: int = 0
    lines: dict[str, dict[str, Any]] = field(default_factory=dict)
    audio: dict[str, bytes] = field(default_factory=dict)
    stream_open: bool = False
    # Each stream connection takes the next number; an older connection ends when replaced.
    stream_generation: int = 0
    complete: bool = False
    stopped: bool = False
    recap_task: asyncio.Task[None] | None = None
    runner_task: asyncio.Task[None] | None = None
    question_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Stops the replay if no stream connects (or reconnects) in time; cancelled when one does.
    stream_deadline_task: asyncio.Task[None] | None = None
    # Line audio started while the line waits for release, keyed by the internal line ID.
    line_audio_tasks: dict[str, asyncio.Task[str | None]] = field(default_factory=dict)
    dispatcher: LiveDispatcher | None = None
    dispatch_source: AsyncIterator[Event] | None = None
    viewer_source: AsyncIterator[Event] | None = None
    # Returned by /start and required (X-Replay-Token) on every call about this replay.
    token: str = field(default_factory=_new_replay_token)
    # The pipeline this replay was started with: the live one, or the cached one when the
    # spending guard had tripped. None means the app's pipeline.
    pipeline: Any = None
    # Off for replays started after the spending guard tripped: no speech at all.
    audio_enabled: bool = True


class QuestionRateLimiter:
    def __init__(self, limit: int = 6, window_seconds: float = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_ip: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        requests = self._requests[client_ip]
        while requests and current - requests[0] >= self.window_seconds:
            requests.popleft()
        if len(requests) >= self.limit:
            retry_after = max(1, math.ceil(self.window_seconds - (current - requests[0])))
            raise HTTPException(
                status_code=429,
                detail="Question limit reached: 6 questions per minute. Please wait and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        requests.append(current)


def _client_ip(request: Request) -> str:
    """The client address that Container Apps ingress itself appended.

    Ingress appends the caller's address to any X-Forwarded-For the client sent, and Learn
    says only the right-most entry comes from Container Apps; the others are client-chosen.
    """
    forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
    if forwarded:
        return forwarded
    if request.client is not None:
        return request.client.host
    return "unknown"


def _event_payload(event: Event, info: MatchInfo) -> dict[str, Any]:
    team = (
        team_name(info, event.team).full
        if event.team is not None
        else None
    )
    squads = (info.home, info.away)
    player_name = next(
        (
            player.get("name")
            for squad in squads
            for player in squad.players
            if player.get("id") == event.player_id
        ),
        None,
    )
    return {
        **event.model_dump(mode="json"),
        "team_name": team,
        "player_name": player_name,
        "clock": event.minute + event.added_minute,
    }


def _safe_explanation(explanation: Explanation, info: MatchInfo) -> tuple[Explanation, str | None]:
    """Filter the model-written answer. Claims and notices are deterministic and never filtered."""
    reason = viewer_output_block_reason([explanation.answer], info)
    if reason is None:
        return explanation, None
    return (
        explanation.model_copy(
            update={
                "answer": "That answer was blocked by the content filter. Ask about match events or tactics.",
                "claims": [],
                "fact_ids": [],
                "event_ids": [],
                "cannot_tell": [],
                "notice": "The answer contained restricted content and was not shown.",
            }
        ),
        reason,
    )


def _evidence_payload(report: EvidenceReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {"passed": report.passed, "failures": list(report.failures)}


def _safe_question_explanation(
    pipeline: PunditPipeline,
    result: QuestionResult,
    info: MatchInfo,
    events: list[Event],
    as_of: int,
) -> tuple[Explanation, str | None]:
    if getattr(result, "source", "agent") != "agent":
        return result.explanation, None
    explanation, reason = _safe_explanation(result.explanation, info)
    if reason is None:
        return explanation, None
    logger.warning("question_answer_withheld reason=%s", reason)
    fallback = pipeline.fallback_question(
        info,
        events,
        as_of,
        result.tool_results,
        "The generated answer was withheld; verified match facts are shown instead.",
        result.attempts,
    )
    return fallback.explanation, reason


def _answer_payload(
    question_id: str,
    transcript: str,
    result: Any,
    explanation: Explanation,
    blocked_reason: str | None,
    started: float,
) -> dict[str, Any]:
    timings = getattr(result, "timings", ())
    return {
        "question_id": question_id,
        "transcript": transcript,
        "explanation": explanation.model_dump(mode="json"),
        "audio_id": None,
        "blocked": blocked_reason is not None,
        "blocked_reason": blocked_reason,
        "source": "fallback" if blocked_reason else getattr(result, "source", "agent"),
        "attempts": getattr(result, "attempts", 0),
        "finish_reasons": [timing.finish_reason for timing in timings],
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def _read_wav_duration(audio_bytes: bytes, *, max_seconds: int = MAX_QUESTION_AUDIO_SECONDS) -> float:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise ValueError("Question audio must be an uncompressed WAV file")
            duration = wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise ValueError("Question audio must be a valid WAV file") from exc
    if duration <= 0:
        raise ValueError("Question audio must not be empty")
    if duration > max_seconds:
        raise ValueError(f"Question audio must be {max_seconds} seconds or shorter")
    return duration


def _encode_sse(event: str, payload: dict[str, Any], message_id: int | None = None) -> bytes:
    data = json.dumps(payload, separators=(",", ":"))
    prefix = f"id: {message_id}\n" if message_id is not None else ""
    return f"{prefix}event: {event}\ndata: {data}\n\n".encode()


def _resume_position(last_event_id: str | None, sent: int) -> int:
    """The outbox position after the browser's Last-Event-ID; 0 for a first connection."""
    if last_event_id is None or not last_event_id.strip():
        return 0
    try:
        position = int(last_event_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a message number.") from exc
    if position < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a message number.")
    return min(position, sent)


def create_app(
    *,
    settings: AgentSettings | None = None,
    pipeline: PunditPipeline | None = None,
    credential: Any | None = None,
    speech_recognizer: SpeechRecognizer | None = None,
    speech_synthesizer: SpeechSynthesizer | None = None,
) -> FastAPI:
    configured_settings = settings or AgentSettings.from_environment()
    limiter = QuestionRateLimiter(
        limit=configured_settings.questions_per_minute,
        window_seconds=configured_settings.question_rate_window_seconds,
    )
    manager = ReplayManager(max_concurrent=configured_settings.max_concurrent_replays)
    guard = SpendingGuard(
        configured_settings.model_calls_per_day,
        configured_settings.tts_characters_per_day,
    )
    sessions: dict[str, ReplaySession] = {}
    client_sessions: dict[str, str] = {}
    pipeline_override = pipeline
    credential_override = credential
    recognizer_override = speech_recognizer
    synthesizer_override = speech_synthesizer

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.settings = configured_settings
        app.state.credential = credential_override
        if app.state.credential is None and (
            configured_settings.mode == "live"
            or configured_settings.speech_endpoint
            or configured_settings.speech_region
        ):
            app.state.credential = DefaultAzureCredential()
        app.state.pipeline = pipeline_override or PunditPipeline(
            configured_settings, app.state.credential
        )
        if isinstance(app.state.pipeline, PunditPipeline):
            app.state.pipeline.on_model_call = guard.record_model_call
        app.state.spending = guard
        app.state.cached_pipeline = None
        app.state.speech_recognizer = recognizer_override
        app.state.speech_synthesizer = synthesizer_override
        app.state.sessions = sessions
        app.state.client_sessions = client_sessions
        app.state.manager = manager
        app.state.rate_limiter = limiter

        if configured_settings.mode == "live":
            app.state.warmup = {"status": "warming", "error": None}

            async def warm_up() -> None:
                try:
                    await app.state.pipeline.warm_up()
                except Exception as exc:
                    logger.exception("model_warm_up_failed")
                    app.state.warmup = {
                        "status": "failed",
                        "error": "Warm-up failed; check server configuration and logs",
                    }
                else:
                    app.state.warmup = {"status": "ready", "error": None}

            app.state.warmup_task = asyncio.create_task(warm_up())
        else:
            app.state.warmup = {"status": "ready", "error": None}
            app.state.warmup_task = None

        try:
            yield
        finally:
            active = list(sessions.values())
            for session in active:
                await _stop_session(session)
            if app.state.warmup_task is not None and not app.state.warmup_task.done():
                app.state.warmup_task.cancel()
                await asyncio.gather(app.state.warmup_task, return_exceptions=True)
            if app.state.credential is not None:
                close = getattr(app.state.credential, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result

    app = FastAPI(title="Second Screen Pundit", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    def cached_pipeline() -> PunditPipeline:
        """Pre-written lines and cached Q&A: no model calls."""
        if app.state.cached_pipeline is None:
            app.state.cached_pipeline = PunditPipeline(
                replace(configured_settings, mode="cached"), None
            )
        return app.state.cached_pipeline

    def session_pipeline(session: ReplaySession) -> Any:
        return session.pipeline if session.pipeline is not None else app.state.pipeline

    def speech_services() -> tuple[SpeechRecognizer, SpeechSynthesizer]:
        if app.state.speech_recognizer is not None and app.state.speech_synthesizer is not None:
            return app.state.speech_recognizer, app.state.speech_synthesizer
        try:
            endpoint, region = configured_settings.require_speech()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="Speech is not configured on this server.",
            ) from exc
        if app.state.credential is None:
            app.state.credential = DefaultAzureCredential()
        app.state.speech_recognizer = SpeechRecognizer(
            endpoint=endpoint,
            region=region,
            credential=app.state.credential,
        )
        app.state.speech_synthesizer = SpeechSynthesizer(
            endpoint=endpoint,
            region=region,
            credential=app.state.credential,
            voice_name=configured_settings.speech_voice,
            fallback_voice_name=configured_settings.speech_fallback_voice,
        )
        return app.state.speech_recognizer, app.state.speech_synthesizer

    async def synthesize(
        session: ReplaySession,
        text: str,
        *,
        tier: DeliveryTier | None = None,
    ) -> str | None:
        if not session.audio_enabled:
            return None
        if not guard.allow_speech(len(text)):
            return None  # the guard logs when it trips; the text is still delivered
        try:
            _, synthesizer = speech_services()
            audio_id = uuid4().hex
            from tempfile import TemporaryDirectory

            def make_audio() -> bytes:
                with TemporaryDirectory(prefix="pundit-audio-") as directory:
                    artifact = synthesizer.synthesize_mp3(
                        text,
                        Path(directory) / f"{audio_id}.mp3",
                        tier=tier,
                    )
                    return artifact.path.read_bytes()

            with tracer.start_as_current_span("pundit.speech.synthesize") as span:
                span.set_attribute("app.match_id", session.info.match_id)
                span.set_attribute("app.speech.characters", len(text))
                span.set_attribute("app.speech.tier", str(tier) if tier is not None else "none")
                async with asyncio.timeout(configured_settings.speech_synthesis_timeout_seconds):
                    session.audio[audio_id] = await asyncio.to_thread(make_audio)
                span.set_attribute("app.speech.bytes", len(session.audio[audio_id]))
            return audio_id
        except TimeoutError:
            logger.warning(
                "speech_synthesis_timeout timeout_seconds=%s",
                configured_settings.speech_synthesis_timeout_seconds,
            )
            return None
        except Exception as exc:
            logger.warning("speech_synthesis_unavailable error=%s", type(exc).__name__)
            return None

    def prepare_line_audio(session: ReplaySession, outcome: WindowOutcome) -> None:
        """Start the line's TTS during the broadcast delay, so text and audio arrive together."""
        line = outcome.result.line if outcome.result is not None else None
        if line is None or session.stopped or not session.audio_enabled:
            return
        session.line_audio_tasks[line.line_id] = asyncio.create_task(
            synthesize(session, line.text, tier=line.delivery_tier)
        )

    async def publish_outcome(session: ReplaySession, outcome: WindowOutcome) -> None:
        if outcome.result is None:
            return
        if outcome.result.insight is not None:
            await session.outbox.put(
                (
                    "insight",
                    {
                        "match_id": session.info.match_id,
                        "insight": outcome.result.insight.model_dump(mode="json"),
                        "event_id": outcome.event.event_id,
                    },
                )
            )
        line = outcome.result.line
        if line is None:
            return
        # A line never reaches the viewer before its event: wait for the viewer stream, not
        # only for the delay, since the two release loops can wake in either order.
        async with session.release_condition:
            await session.release_condition.wait_for(
                lambda: session.viewer_released_as_of >= outcome.event.seq
            )
        public_line_id = f"{session.replay_id}-{line.line_id}"
        public_line = line.model_copy(update={"line_id": public_line_id})
        events_by_id = {event.event_id: event for event in session.events}
        cited_events = [
            events_by_id[event_id]
            for event_id in line.event_ids
            if event_id in events_by_id and events_by_id[event_id].seq <= outcome.event.seq
        ]
        facts = [
            fact.model_dump(mode="json")
            for result in outcome.result.tool_results
            for fact in result.facts
            if fact.fact_id in line.fact_ids
        ]
        session.lines[public_line_id] = {
            "line": public_line.model_dump(mode="json"),
            "insight": (
                outcome.result.insight.model_dump(mode="json")
                if outcome.result.insight is not None
                else None
            ),
            "facts": facts,
            "events": [_event_payload(event, session.info) for event in cited_events],
            "agent": line.source,
            "evidence": _evidence_payload(outcome.result.evidence),
        }
        audio_task = session.line_audio_tasks.pop(line.line_id, None)
        if not session.audio_enabled:
            await session.outbox.put(
                (
                    "commentary",
                    {
                        "line": public_line.model_dump(mode="json"),
                        "audio_id": None,
                        "audio_available": False,
                        "audio_pending": False,
                    },
                )
            )
            return
        if audio_task is not None and audio_task.done() and not audio_task.cancelled():
            # The usual case: the audio was ready before the line's release.
            audio_id = audio_task.result()
            await session.outbox.put(
                (
                    "commentary",
                    {
                        "line": public_line.model_dump(mode="json"),
                        "audio_id": audio_id,
                        "audio_available": audio_id is not None,
                        "audio_pending": False,
                    },
                )
            )
            return
        # The text never waits for its audio.
        await session.outbox.put(
            (
                "commentary",
                {
                    "line": public_line.model_dump(mode="json"),
                    "audio_id": None,
                    "audio_available": False,
                    "audio_pending": True,
                },
            )
        )
        audio_id = (
            await audio_task
            if audio_task is not None
            else await synthesize(session, line.text, tier=line.delivery_tier)
        )
        await session.outbox.put(
            (
                "commentary_audio" if audio_id is not None else "commentary_audio_unavailable",
                {"line_id": public_line_id, "audio_id": audio_id},
            )
        )

    async def make_recap(session: ReplaySession, as_of: int) -> None:
        try:
            with turn_span("recap", session.info.match_id) as span:
                span.set_attribute("app.as_of", as_of)
                result = await session_pipeline(session).produce_recap(
                    session.info,
                    session.events,
                    as_of=as_of,
                )
                record_result(span, result)
            payload = {
                "recap": result.recap.model_dump(mode="json"),
                "audio_id": None,
                "audio_available": False,
                "audio_pending": session.audio_enabled,
                "evidence": _evidence_payload(result.evidence),
            }
            await session.outbox.put(("recap", payload))
            if not session.audio_enabled:
                return
            audio_id = await synthesize(session, result.recap.audio_script)
            await session.outbox.put(
                (
                    "recap_audio" if audio_id is not None else "recap_audio_unavailable",
                    {"audio_id": audio_id},
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("recap_failed match_id=%s", session.info.match_id)
            await session.outbox.put(
                (
                    "error",
                    {"message": "The full-time recap is unavailable."},
                )
            )

    async def release_events(session: ReplaySession, source: AsyncIterator[Event]) -> None:
        async for event in session.delay.events(source):
            async with session.release_condition:
                session.viewer_released_as_of = session.delay.released_as_of
                session.released_events.append(event)
                await session.outbox.put(("event", _event_payload(event, session.info)))
                session.release_condition.notify_all()
            if event.type == "full_time":
                session.recap_task = asyncio.create_task(make_recap(session, event.seq))

    async def run_session(session: ReplaySession) -> None:
        if session.dispatcher is None or session.dispatch_source is None or session.viewer_source is None:
            raise RuntimeError("Replay session workers were not initialized")
        dispatch_task = asyncio.create_task(
            session.dispatcher.run(
                session.dispatch_source,
                replay_stopped=lambda: session.replay.state == "stopped",
            )
        )
        release_task = asyncio.create_task(release_events(session, session.viewer_source))
        children = (dispatch_task, release_task)
        try:
            results = await asyncio.gather(*children, return_exceptions=True)
            worker_errors = [
                result for result in results if isinstance(result, BaseException)
            ]
            if session.recap_task is not None:
                try:
                    await session.recap_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    worker_errors.append(exc)
            if worker_errors:
                failure = worker_errors[0]
                logger.error(
                    "replay_worker_failed replay_id=%s error=%s",
                    session.replay_id,
                    type(failure).__name__,
                )
                await session.outbox.put(
                    (
                        "error",
                        {
                            "message": (
                                "The replay stopped unexpectedly. The match can be started again."
                            )
                        },
                    )
                )
            session.complete = True
            await session.outbox.put(("complete", {"replay_id": session.replay_id}))
        except asyncio.CancelledError:
            raise
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    async def stop_without_stream(
        session: ReplaySession, seconds: float, reason: str = "replay_stream_deadline"
    ) -> None:
        """A replay nobody is watching frees its slot instead of spending model calls."""
        await asyncio.sleep(seconds)
        if not session.stream_open and not session.stopped:
            logger.warning("%s replay_id=%s seconds=%s", reason, session.replay_id, seconds)
            session.stream_deadline_task = None  # don't cancel the task that is stopping it
            await _stop_session(session)

    async def _stop_session(session: ReplaySession) -> None:
        if session.stopped:
            return
        session.stopped = True
        deadline = session.stream_deadline_task
        session.stream_deadline_task = None
        if deadline is not None and not deadline.done():
            deadline.cancel()
        session.outbox.close()
        await session.replay.stop()
        if session.runner_task is not None and not session.runner_task.done():
            session.runner_task.cancel()
            await asyncio.gather(session.runner_task, return_exceptions=True)
        if session.recap_task is not None and not session.recap_task.done():
            session.recap_task.cancel()
            await asyncio.gather(session.recap_task, return_exceptions=True)
        for task in list(session.question_tasks):
            task.cancel()
        await asyncio.gather(*session.question_tasks, return_exceptions=True)
        audio_tasks = list(session.line_audio_tasks.values())
        session.line_audio_tasks.clear()
        for task in audio_tasks:
            task.cancel()
        await asyncio.gather(*audio_tasks, return_exceptions=True)
        session.audio.clear()
        sessions.pop(session.replay_id, None)
        if client_sessions.get(session.client_id) == session.replay_id:
            client_sessions.pop(session.client_id, None)

    def find_session(replay_id: str, request: Request) -> ReplaySession:
        """The replay, if the caller holds its token.

        The replay ID shows up in URLs; the token (X-Replay-Token, 192 random bits) is only in
        the /start response and the page's memory. It doesn't depend on the caller's address,
        so a phone that changes network keeps its replay. A missing or wrong token gets the
        same 404 as a missing replay.
        """
        session = sessions.get(replay_id)
        token = request.headers.get("x-replay-token", "")
        if (
            session is None
            or session.stopped
            or not secrets.compare_digest(token.encode(), session.token.encode())
        ):
            raise HTTPException(status_code=404, detail="Replay not found or already stopped.")
        return session

    def check_question_access(replay_id: str, request: Request) -> ReplaySession:
        session = find_session(replay_id, request)
        if session.viewer_released_as_of <= 0:
            raise HTTPException(
                status_code=409,
                detail="The viewer has not received a match event yet. Please try again shortly.",
            )
        limiter.check(_client_ip(request))
        return session

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/api/matches")
    async def matches() -> list[dict[str, str]]:
        result = []
        for match_id in ("m01", "m02", "m03"):
            info = get_match_info(match_id)
            result.append(
                {
                    "id": match_id,
                    "label": f"{info.home.name} v {info.away.name}",
                    "home": info.home.name,
                    "away": info.away.name,
                    "stadium": info.stadium,
                }
            )
        return result

    @app.post("/api/matches/{match_id}/start")
    async def start_match(
        match_id: str,
        request: Request,
        speed: int = 1,
    ) -> dict[str, Any]:
        warmup = app.state.warmup
        limited = guard.tripped
        if warmup["status"] != "ready" and not limited:
            message = (
                "Pundit warm-up failed; check the server configuration and logs."
                if warmup["status"] == "failed"
                else "Pundit warming up; Start will be available when it is ready."
            )
            raise HTTPException(
                status_code=503,
                detail=message,
                headers={"Retry-After": "5"},
            )
        client_id = request.headers.get("x-client-id", "").strip()
        if not client_id or len(client_id) > 80:
            raise HTTPException(status_code=400, detail="A valid X-Client-ID header is required.")
        if not 1 <= speed <= 120:
            raise HTTPException(status_code=422, detail="Replay speed must be between 1 and 120.")
        client_ip = _client_ip(request)
        # One replay per viewer: a new start replaces this viewer's earlier replay, so a lost
        # stream or a reload never locks them out. Other viewers on the same network keep theirs.
        for previous in [
            item for item in sessions.values() if item.client_id == client_id and not item.stopped
        ]:
            logger.info(
                "replay_replaced replay_id=%s client_has_new_replay=true", previous.replay_id
            )
            await _stop_session(previous)
        # Up to MAX_REPLAYS_PER_ADDRESS running replays per address, so one address can't take
        # every slot by inventing client IDs. Finished replays (tab still open) don't count.
        running_here = sum(
            1
            for item in sessions.values()
            if item.client_ip == client_ip
            and not item.stopped
            and item.replay.state not in {"finished", "stopped"}
        )
        if running_here >= MAX_REPLAYS_PER_ADDRESS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"{MAX_REPLAYS_PER_ADDRESS} replays are already running from your network. "
                    "Try again when one has finished."
                ),
                headers={"Retry-After": "30"},
            )
        try:
            info = get_match_info(match_id)
            events = generate_match(match_id)
            replay = manager.prepare(events, speed)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "Maximum concurrent replays" in str(exc):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"All {configured_settings.max_concurrent_replays} replay slots are in use. "
                        "Try again when a match has finished."
                    ),
                    headers={"Retry-After": "10"},
                ) from exc
            raise

        replay_id = uuid4().hex
        delay = BroadcastDelay(configured_settings.broadcast_delay_seconds)
        session = ReplaySession(
            replay_id=replay_id,
            client_id=client_id,
            client_ip=client_ip,
            info=info,
            events=events,
            replay=replay,
            delay=delay,
            pipeline=cached_pipeline() if limited else None,
            audio_enabled=not limited,
        )
        if limited:
            logger.warning(
                "replay_started_limited replay_id=%s mode=cached audio=off", replay_id
            )
        session.dispatcher = LiveDispatcher(
            session_pipeline(session),
            info,
            events,
            configured_settings,
            delay=delay,
            on_outcome=lambda outcome: publish_outcome(session, outcome),
            on_line_ready=lambda outcome: prepare_line_audio(session, outcome),
        )
        session.dispatch_source = replay.subscribe()
        session.viewer_source = replay.subscribe()
        sessions[replay_id] = session
        client_sessions[client_id] = replay_id
        replay.start()
        session.runner_task = asyncio.create_task(run_session(session))
        session.stream_deadline_task = asyncio.create_task(
            stop_without_stream(session, configured_settings.stream_connect_deadline_seconds)
        )
        return {
            "replay_id": replay_id,
            "replay_token": session.token,
            "match_id": match_id,
            "speed": speed,
            "state": replay.state,
            "mode": "cached" if limited else configured_settings.mode,
            "audio": session.audio_enabled,
            "notice": CACHED_NOTICE if limited else None,
        }

    @app.post("/api/replays/{replay_id}/control")
    async def control(replay_id: str, request: Request) -> dict[str, str]:
        session = find_session(replay_id, request)
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Control body must be valid JSON.") from exc
        action = body.get("action") if isinstance(body, dict) else None
        if action not in {"pause", "resume"}:
            raise HTTPException(status_code=422, detail="Action must be pause or resume.")
        state = await session.replay.control(action)
        # The viewer's delayed stream pauses with the replay, so nothing more arrives.
        if state == "paused":
            session.delay.pause()
        elif state == "running":
            session.delay.resume()
        return {"replay_id": replay_id, "state": state}

    async def answer(
        session: ReplaySession,
        question_id: str,
        question: str,
        started: float,
    ) -> dict[str, Any]:
        as_of = session.viewer_released_as_of
        pipeline = session_pipeline(session)
        if guard.models_exhausted and pipeline is app.state.pipeline:
            pipeline = cached_pipeline()
        with turn_span("question", session.info.match_id) as span:
            span.set_attribute("app.as_of", as_of)
            span.set_attribute("app.question.characters", len(question))
            result = await pipeline.answer_question(
                session.info,
                session.events,
                question,
                as_of=as_of,
            )
            explanation, blocked_reason = _safe_question_explanation(
                pipeline,
                result,
                session.info,
                session.events,
                as_of,
            )
            record_result(span, result)
            span.set_attribute("app.answer.blocked", blocked_reason is not None)
        return _answer_payload(question_id, question, result, explanation, blocked_reason, started)

    @app.post("/api/ask")
    async def ask(payload: QuestionRequest, request: Request) -> dict[str, Any]:
        started = time.perf_counter()
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="Question must not be empty.")
        if len(question) > configured_settings.question_max_characters:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Question must be {configured_settings.question_max_characters} "
                    "characters or shorter."
                ),
            )
        session = check_question_access(payload.replay_id, request)
        question_id = uuid4().hex
        result = await answer(session, question_id, question, started)
        if not session.audio_enabled:
            return {**result, "audio_pending": False}
        # The text goes back at once; the spoken answer follows on the replay's SSE stream,
        # the same way as for a spoken question.
        task = asyncio.create_task(speak_answer(session, question_id, result["explanation"]["answer"]))
        session.question_tasks.add(task)
        task.add_done_callback(session.question_tasks.discard)
        return {**result, "audio_pending": True}

    async def speak_answer(session: ReplaySession, question_id: str, text: str) -> None:
        audio_id = await synthesize(session, text)
        await session.outbox.put(
            (
                "answer_audio" if audio_id is not None else "answer_audio_unavailable",
                {"question_id": question_id, "audio_id": audio_id},
            )
        )

    async def answer_spoken(
        session: ReplaySession,
        question_id: str,
        transcript: str,
        started: float,
        budget: float,
    ) -> None:
        """Send the answer text over SSE as soon as it is checked, then its audio."""
        try:
            async with asyncio.timeout(budget):
                payload = await answer(session, question_id, transcript, started)
        except TimeoutError:
            await session.outbox.put(
                (
                    "answer_error",
                    {
                        "question_id": question_id,
                        "message": "The spoken question timed out. Please try typing it instead.",
                    },
                )
            )
            return
        except Exception:
            logger.exception("spoken_question_failed question_id=%s", question_id)
            await session.outbox.put(
                (
                    "answer_error",
                    {
                        "question_id": question_id,
                        "message": "The answer is unavailable; please retry or type the question.",
                    },
                )
            )
            return
        await session.outbox.put(("answer", {**payload, "audio_pending": True}))
        await speak_answer(session, question_id, payload["explanation"]["answer"])

    @app.post("/api/ask-audio", status_code=202)
    async def ask_audio(
        request: Request,
        replay_id: str = Form(...),
        audio: UploadFile = File(...),
    ) -> dict[str, Any]:
        """Transcribe, return the transcript at once, and answer over the replay's SSE stream."""
        started = time.perf_counter()
        session = check_question_access(replay_id, request)
        if not session.audio_enabled:
            raise HTTPException(
                status_code=409,
                detail="Spoken questions are off for this match. Please type your question.",
            )
        audio_bytes = await audio.read(configured_settings.question_audio_max_bytes + 1)
        await audio.close()
        if len(audio_bytes) > configured_settings.question_audio_max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Question audio must be "
                    f"{configured_settings.question_audio_max_bytes} bytes or smaller."
                ),
            )
        try:
            _read_wav_duration(
                audio_bytes,
                max_seconds=configured_settings.question_audio_max_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            recognizer, _ = speech_services()
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("speech_recognition_unavailable error=%s", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail="Speech recognition is unavailable; please type the question.",
            ) from exc
        limit = configured_settings.ask_audio_timeout_seconds
        try:
            with tracer.start_as_current_span("pundit.speech.recognize") as span:
                span.set_attribute("app.match_id", session.info.match_id)
                span.set_attribute("app.speech.bytes", len(audio_bytes))
                async with asyncio.timeout(limit):
                    transcript = (
                        await asyncio.to_thread(recognizer.recognize_wav, audio_bytes)
                    ).strip()
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="Spoken question timed out. Please try typing it instead.",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("speech_recognition_failed error=%s", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="Speech recognition failed; please retry or type the question.",
            ) from exc
        if not transcript:
            raise HTTPException(status_code=422, detail="No speech was recognized.")
        if len(transcript) > configured_settings.question_max_characters:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Speech transcript must be "
                    f"{configured_settings.question_max_characters} characters or shorter."
                ),
            )
        question_id = uuid4().hex
        remaining = max(1.0, limit - (time.perf_counter() - started))
        task = asyncio.create_task(
            answer_spoken(session, question_id, transcript, started, remaining)
        )
        session.question_tasks.add(task)
        task.add_done_callback(session.question_tasks.discard)
        return {
            "question_id": question_id,
            "transcript": transcript,
            "status": "answering",
            "stt_ms": round((time.perf_counter() - started) * 1000),
        }

    @app.get("/api/lines/{line_id}/evidence")
    async def line_evidence(line_id: str) -> dict[str, Any]:
        for session in sessions.values():
            if line_id in session.lines:
                return session.lines[line_id]
        raise HTTPException(status_code=404, detail="Commentary evidence is no longer available.")

    @app.get("/api/audio/{audio_id}")
    async def audio(audio_id: str) -> Response:
        for session in sessions.values():
            data = session.audio.get(audio_id)
            if data is not None:
                return Response(
                    content=data,
                    media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"},
                )
        raise HTTPException(status_code=404, detail="Audio is no longer available.")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        warmup = app.state.warmup
        return JSONResponse(
            {
                "status": "ok",
                "warmup": warmup["status"],
                "ready": warmup["status"] == "ready" or guard.tripped,
                "error": warmup["error"],
                # True once today's spending cap is reached: new replays are cached, audio off.
                "limited": guard.tripped,
                "limits": {
                    "question_characters": configured_settings.question_max_characters,
                    "question_audio_seconds": configured_settings.question_audio_max_seconds,
                    "question_audio_bytes": configured_settings.question_audio_max_bytes,
                    "questions_per_minute": configured_settings.questions_per_minute,
                    "question_rate_window_seconds": configured_settings.question_rate_window_seconds,
                    "concurrent_replays": configured_settings.max_concurrent_replays,
                },
            }
        )

    @app.get("/api/replays/{replay_id}/stream")
    async def stream(
        replay_id: str,
        request: Request,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        """The replay's viewer stream; a reconnect with Last-Event-ID resumes after it.

        Every message has an `id:` (its outbox position). A dropped stream doesn't stop the
        replay: it keeps running, paused or not, for STREAM_RECONNECT_GRACE_SECONDS. Each
        connection closes itself after STREAM_MAX_SECONDS, inside the ingress request timeout.
        """
        session = find_session(replay_id, request)
        position = _resume_position(last_event_id, len(session.outbox))
        # The newest connection wins: an older one (often a dead socket nobody has noticed
        # yet) ends instead of blocking the reconnect.
        session.stream_generation += 1
        generation = session.stream_generation
        session.outbox.wake()
        session.stream_open = True
        if session.stream_deadline_task is not None:
            session.stream_deadline_task.cancel()
            session.stream_deadline_task = None
        if position:
            logger.info("stream_resumed replay_id=%s after=%s", replay_id, position)

        async def events() -> AsyncIterator[bytes]:
            nonlocal position
            closes_at = time.monotonic() + configured_settings.stream_max_seconds
            try:
                yield f"retry: {SSE_RETRY_MILLISECONDS}\n\n".encode()
                while session.stream_generation == generation and not session.outbox.closed:
                    remaining = closes_at - time.monotonic()
                    if remaining <= 0:
                        # Close cleanly before ingress cuts the request; the browser reconnects.
                        return
                    try:
                        messages = await session.outbox.read(
                            position, timeout=min(SSE_HEARTBEAT_SECONDS, remaining)
                        )
                    except TimeoutError:
                        if time.monotonic() < closes_at:
                            yield b": heartbeat\n\n"
                        continue
                    for message_id, name, payload in messages:
                        yield _encode_sse(name, payload, message_id)
                        position = message_id
            finally:
                if session.stream_generation == generation and not session.stopped:
                    session.stream_open = False
                    session.stream_deadline_task = asyncio.create_task(
                        stop_without_stream(
                            session,
                            configured_settings.stream_reconnect_grace_seconds,
                            "replay_stream_not_reconnected",
                        )
                    )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


app = create_app()
