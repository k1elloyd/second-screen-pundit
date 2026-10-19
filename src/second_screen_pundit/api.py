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
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from second_screen_pundit.agents.colour import (
    ColourLineCandidate,
    ColourLineGate,
    build_colour_candidate,
)
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.content_filter import viewer_output_block_reason
from second_screen_pundit.agents.dispatcher import LiveDispatcher, WindowOutcome
from second_screen_pundit.agents.evidence import EvidenceReport
from second_screen_pundit.agents.models import DeliveryTier, Explanation
from second_screen_pundit.agents.pipeline import PunditPipeline, QuestionResult, is_must_say
from second_screen_pundit.agents.tools.analytics import possession_pair
from second_screen_pundit.agents.speech import SpeechRecognizer, SpeechSynthesizer
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay, ReplayManager
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import short_name, team_name
from second_screen_pundit.observability import record_result, tracer, turn_span
from second_screen_pundit.outbox import ViewerOutbox
from second_screen_pundit.profile import ProfileMode, ProfileTeam, ViewerProfile
from second_screen_pundit.spending import CACHED_NOTICE, SpendingGuard


logger = logging.getLogger(__name__)
MAX_QUESTION_AUDIO_BYTES = 1_000_000
MAX_QUESTION_AUDIO_SECONDS = 15
BUSY_MESSAGE = "The pundit is busy, try again in a minute."
# Replays one network address may run at once, within the global cap: judges in one office
# share an address.
MAX_REPLAYS_PER_ADDRESS = 3
_WEB_DIR = Path(__file__).with_name("web")
SSE_HEARTBEAT_SECONDS = 15
# How long the browser waits before reconnecting a stream that closed or dropped.
SSE_RETRY_MILLISECONDS = 1000
# Seconds before each warm-up retry after a failure; the last value repeats until it succeeds.
WARMUP_RETRY_SECONDS: tuple[float, ...] = (5, 15, 30, 60)
# Least real time between colour-line attempts; each attempt costs about a second of CPU.
COLOUR_RETRY_SECONDS = 2.0
# A tactical or colour line whose window ended more match minutes than this before the latest
# published line is dropped: it would read out of order in the feed.
STALE_LINE_MINUTES = 2
WARMUP_NOTICE = (
    "The live pundit isn't available right now, so this match uses pre-written commentary "
    "with audio off. Live commentary returns as soon as it reconnects."
)


class QuestionRequest(BaseModel):
    replay_id: str
    question: str
    # Chosen by the page, so an "answer_checking" event on the stream can find its card
    # before the answer comes back.
    question_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class ViewerProfileRequest(BaseModel):
    mode: ProfileMode
    team: ProfileTeam


class ViewerPlaybackStateRequest(BaseModel):
    audio_busy: bool
    revision: int = Field(ge=0)


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
    # Successful passes per side among released events: the match possession the page shows.
    released_passes: dict[str, int] = field(default_factory=lambda: {"home": 0, "away": 0})
    lines: dict[str, dict[str, Any]] = field(default_factory=dict)
    audio: dict[str, bytes] = field(default_factory=dict)
    stream_open: bool = False
    # When the stream last dropped (monotonic), None while one is connected: when the app is
    # full, the replay whose viewer has been gone longest gives up its slot.
    away_since: float | None = None
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
    colour_gate: ColourLineGate = field(default_factory=ColourLineGate)
    # The gate's (generation, events_seen) at the last colour attempt, and when it started:
    # a new attempt needs something to have changed and COLOUR_RETRY_SECONDS to have passed.
    colour_attempt_key: tuple[int, int] | None = None
    colour_attempted_at: float = -math.inf
    viewer_line_state_revision: int = -1
    pending_real_events: set[int] = field(default_factory=set)
    # The match minute of the latest line published to the viewer, for the stale-line rule.
    latest_line_minute: int | None = None
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
    # Mode and My team, sent with Start and changeable mid-match (from the next line).
    profile: ViewerProfile = field(default_factory=ViewerProfile)


class QuestionRateLimiter:
    """Questions per viewer per window, inside a higher ceiling per network address.

    Judges on one office network share an address, so the per-viewer limit is keyed on the
    viewer (X-Client-ID, held on their replay). Rotating client IDs from one address still
    meets the address ceiling. Without a client ID, the address gets the per-viewer limit.
    """

    def __init__(
        self,
        limit: int = 6,
        window_seconds: float = 60,
        address_limit: int | None = None,
    ) -> None:
        self.limit = limit
        self.address_limit = limit if address_limit is None else address_limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, current: float) -> deque[float]:
        requests = self._requests[key]
        while requests and current - requests[0] >= self.window_seconds:
            requests.popleft()
        return requests

    def _refuse(self, requests: deque[float], current: float, detail: str) -> None:
        retry_after = max(1, math.ceil(self.window_seconds - (current - requests[0])))
        raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})

    def check(
        self, client_ip: str, *, client_id: str | None = None, now: float | None = None
    ) -> None:
        current = time.monotonic() if now is None else now
        if client_id is None:
            limits = [(f"address:{client_ip}", self.limit)]
        else:
            limits = [(f"client:{client_id}", self.limit), (f"address:{client_ip}", self.address_limit)]
        # Check every limit before counting, so a refused question uses up nothing.
        for key, limit in limits:
            requests = self._recent(key, current)
            if len(requests) >= limit:
                per_viewer = key.startswith("client:") or client_id is None
                self._refuse(
                    requests,
                    current,
                    f"Question limit reached: {limit} questions per minute. Please wait and try again."
                    if per_viewer
                    else "Your network has asked a lot of questions in the last minute. "
                    "Please wait a moment and try again.",
                )
        for key, _limit in limits:
            self._requests[key].append(current)


def is_stale_line(path: str, minute: int, latest_line_minute: int | None) -> bool:
    """A tactical or colour line about a window that ended well before the latest line.

    A slow tactical window (or a colour line) can be ready after a later line has gone out;
    published then, the feed would read 24' after 29'. Fast-path lines (goals, cards,
    substitutions, formation changes, big chances) are never stale.
    """
    return (
        path != "fast_path"
        and latest_line_minute is not None
        and latest_line_minute - minute > STALE_LINE_MINUTES
    )


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


def _released_possession(session: ReplaySession, event: Event) -> dict[str, int] | None:
    """Match possession as of this event, whole numbers from possession_pair (they sum to 100).

    The same measure as the analytics: each side's share of successful passes. None before the
    first successful pass.
    """
    if event.type == "pass" and event.outcome == "success" and event.team is not None:
        session.released_passes[event.team] += 1
    total = sum(session.released_passes.values())
    if not total:
        return None
    pair = possession_pair(session.released_passes["home"] / total * 100)
    return {side: whole for side, (_, whole) in pair.items()}


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
    warmup_retry_seconds: Sequence[float] = WARMUP_RETRY_SECONDS,
) -> FastAPI:
    if not warmup_retry_seconds:
        raise ValueError("warmup_retry_seconds must not be empty")
    configured_settings = settings or AgentSettings.from_environment()
    limiter = QuestionRateLimiter(
        limit=configured_settings.questions_per_minute,
        window_seconds=configured_settings.question_rate_window_seconds,
        address_limit=configured_settings.questions_per_address_per_minute,
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
                # A failed warm-up is retried with backoff for as long as the app runs: a
                # restart that meets a slow model or identity endpoint must not leave the app
                # unusable. Meanwhile Start serves cached mode (see `degraded`).
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        await app.state.pipeline.warm_up()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        delay = warmup_retry_seconds[min(attempt, len(warmup_retry_seconds)) - 1]
                        if attempt == 1:
                            logger.exception("model_warm_up_failed attempt=1 retry_in=%s", delay)
                        else:
                            logger.warning(
                                "model_warm_up_failed attempt=%d retry_in=%s error=%s",
                                attempt,
                                delay,
                                type(exc).__name__,
                            )
                        app.state.warmup = {
                            "status": "failed",
                            "error": "Warm-up failed; retrying.",
                        }
                        await asyncio.sleep(delay)
                    else:
                        if attempt > 1:
                            logger.warning("model_warm_up_recovered attempts=%d", attempt)
                        app.state.warmup = {"status": "ready", "error": None}
                        return

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

    def warm_up_failed() -> bool:
        """The live model's warm-up failed and is being retried: new viewers get cached mode."""
        return app.state.warmup["status"] == "failed"

    def session_pipeline(session: ReplaySession) -> Any:
        return session.pipeline if session.pipeline is not None else app.state.pipeline

    def note_colour_event(session: ReplaySession, event: Event) -> None:
        session.colour_gate.record_event()
        if is_must_say(event, session.events):
            session.pending_real_events.add(event.seq)

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

    async def publish_outcome(
        session: ReplaySession,
        outcome: WindowOutcome,
        *,
        colour_generation: int | None = None,
        colour_candidate: ColourLineCandidate | None = None,
    ) -> bool:
        if outcome.result is None:
            return False
        line = outcome.result.line
        if line is None and outcome.result.insight is None:
            return False
        # Nothing about a window reaches the viewer before its event, the insight included
        # (its headline and event IDs would be a spoiler on the stream): wait for the viewer
        # stream, not only for the delay, since the two release loops can wake in either order.
        async with session.release_condition:
            await session.release_condition.wait_for(
                lambda: session.viewer_released_as_of >= outcome.event.seq
            )
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
            if line is None:
                return False
            if colour_generation is not None and (
                not session.colour_gate.candidate_is_current(colour_generation)
                or bool(session.pending_real_events)
                or not session.stream_open
                or session.replay.state != "running"
                or not session.colour_gate.is_due(
                    time.monotonic(),
                    outcome.minute,
                    event_pending=False,
                    stream_open=session.stream_open,
                    replay_running=session.replay.state == "running",
                )
            ):
                audio_task = session.line_audio_tasks.pop(line.line_id, None)
                if audio_task is not None and not audio_task.done():
                    audio_task.cancel()
                return False
            if is_stale_line(outcome.path, outcome.minute, session.latest_line_minute):
                logger.info(
                    "stale_line_dropped match_id=%s line_id=%s minute=%d latest_line_minute=%s "
                    "source=%s",
                    session.info.match_id,
                    line.line_id,
                    outcome.minute,
                    session.latest_line_minute,
                    line.source,
                )
                audio_task = session.line_audio_tasks.pop(line.line_id, None)
                if audio_task is not None and not audio_task.done():
                    audio_task.cancel()
                return False
            # Checked and recorded under the release lock, so two lines can't both pass
            # against the same latest minute.
            session.latest_line_minute = max(session.latest_line_minute or 0, outcome.minute)
        public_line_id = f"{session.replay_id}-{line.line_id}"
        # The side the line is about, for the page's My team accent: a must-say event's team,
        # or the one team behind a tactical window's signals. None for both or neither.
        signal_teams = {signal.team for signal in outcome.signals}
        line_team = (
            outcome.event.team
            if outcome.path == "fast_path"
            else signal_teams.pop() if len(signal_teams) == 1 else None
        )
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
            record_commentary_release(session, outcome, colour_candidate)
            await session.outbox.put(
                (
                    "commentary",
                    {
                        "line": public_line.model_dump(mode="json"),
                        "line_team": line_team,
                        "audio_id": None,
                        "audio_available": False,
                        "audio_pending": False,
                    },
                )
            )
            return True
        if audio_task is not None and audio_task.done() and not audio_task.cancelled():
            # The usual case: the audio was ready before the line's release.
            audio_id = audio_task.result()
            record_commentary_release(session, outcome, colour_candidate)
            await session.outbox.put(
                (
                    "commentary",
                    {
                        "line": public_line.model_dump(mode="json"),
                        "line_team": line_team,
                        "audio_id": audio_id,
                        "audio_available": audio_id is not None,
                        "audio_pending": False,
                    },
                )
            )
            return True
        # The text never waits for its audio.
        record_commentary_release(session, outcome, colour_candidate)
        await session.outbox.put(
            (
                "commentary",
                {
                    "line": public_line.model_dump(mode="json"),
                    "line_team": line_team,
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
        return True

    def record_commentary_release(
        session: ReplaySession,
        outcome: WindowOutcome,
        colour_candidate: ColourLineCandidate | None,
    ) -> None:
        now = time.monotonic()
        if colour_candidate is None:
            session.colour_gate.record_real_line(now, audio_enabled=session.audio_enabled)
        else:
            session.colour_gate.record_colour_release(
                colour_candidate.version,
                colour_candidate.statistic_keys,
                outcome.minute,
                now,
                audio_enabled=session.audio_enabled,
            )

    async def make_recap(session: ReplaySession, as_of: int) -> None:
        try:
            with turn_span("recap", session.info.match_id, session.profile) as span:
                span.set_attribute("app.as_of", as_of)
                result = await session_pipeline(session).produce_recap(
                    session.info,
                    session.events,
                    as_of=as_of,
                    profile=session.profile,
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
                payload = _event_payload(event, session.info)
                payload["possession"] = _released_possession(session, event)
                await session.outbox.put(("event", payload))
                session.pending_real_events.discard(event.seq)
                session.release_condition.notify_all()
            if event.type == "full_time":
                session.recap_task = asyncio.create_task(make_recap(session, event.seq))

    def colour_overtaken(session: ReplaySession, as_of: int) -> bool:
        """A shot or a must-say event reached the viewer after `as_of`."""
        for event in reversed(session.released_events):
            if event.seq <= as_of:
                return False
            if event.type == "shot" or is_must_say(event, session.events):
                return True
        return False

    async def colour_lines(session: ReplaySession) -> None:
        while not session.stopped and session.replay.state in {"running", "paused"}:
            await asyncio.sleep(0.05)
            if session.replay.state != "running" or not session.stream_open:
                continue
            as_of = session.viewer_released_as_of
            if as_of <= 0:
                continue
            line_event = next(
                (event for event in reversed(session.released_events) if event.seq == as_of),
                None,
            )
            if line_event is None:
                continue
            match_minute = min(90, line_event.minute + line_event.added_minute)
            gate = session.colour_gate
            generation = gate.generation
            attempt_key = (generation, gate.events_seen)
            now = time.monotonic()
            if (
                attempt_key == session.colour_attempt_key
                or now - session.colour_attempted_at < COLOUR_RETRY_SECONDS
            ):
                continue
            if not gate.is_due(
                now,
                match_minute,
                event_pending=bool(session.pending_real_events),
                stream_open=session.stream_open,
                replay_running=session.replay.state == "running",
            ):
                continue
            session.colour_attempt_key = attempt_key
            session.colour_attempted_at = now
            line_pipeline = session_pipeline(session)
            if not isinstance(line_pipeline, PunditPipeline):
                continue
            try:
                # About a second of CPU over the match so far: in a worker thread, so every
                # viewer's stream, audio and questions keep moving meanwhile. The match's
                # events never change; the used sets are copied because a release on the
                # loop adds to them.
                candidate = await asyncio.to_thread(
                    build_colour_candidate,
                    session.info,
                    session.events,
                    as_of=as_of,
                    ordinal=gate.count + 1,
                    used_versions=set(gate.used_versions),
                    used_statistics=set(gate.used_statistics),
                    mode=session.profile.mode,
                )
                if candidate is None or session.stopped:
                    continue
                if colour_overtaken(session, as_of):
                    # The viewer saw a shot or a must-say event while it was being built,
                    # so its statistics could already read as out of date.
                    continue
                result = line_pipeline.validate_colour_line(
                    candidate.line,
                    candidate.insight,
                    info=session.info,
                    events=session.events,
                    as_of=as_of,
                    tool_results=list(candidate.tool_results),
                    lane=candidate.lane,
                )
            except Exception:
                logger.exception(
                    "colour_line_failed match_id=%s as_of=%d",
                    session.info.match_id,
                    as_of,
                )
                continue
            if (
                result.status != "commentary"
                or result.line is None
                or result.evidence is None
            ):
                continue
            outcome = WindowOutcome(
                path="tactical",
                event=line_event,
                minute=min(90, line_event.minute + line_event.added_minute),
                result=replace(result, insight=None),
            )
            prepare_line_audio(session, outcome)
            await publish_outcome(
                session,
                outcome,
                colour_generation=generation,
                colour_candidate=candidate,
            )

    async def run_session(session: ReplaySession) -> None:
        if session.dispatcher is None or session.dispatch_source is None or session.viewer_source is None:
            raise RuntimeError("Replay session workers were not initialized")

        async def observed_events(source: AsyncIterator[Event]) -> AsyncIterator[Event]:
            async for event in source:
                session.delay.stamp(event)
                note_colour_event(session, event)
                yield event

        dispatch_task = asyncio.create_task(
            session.dispatcher.run(
                observed_events(session.dispatch_source),
                replay_stopped=lambda: session.replay.state == "stopped",
            )
        )
        release_task = asyncio.create_task(release_events(session, session.viewer_source))
        colour_task = asyncio.create_task(colour_lines(session))
        children = (dispatch_task, release_task, colour_task)
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
        limiter.check(_client_ip(request), client_id=session.client_id)
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
                    "home_short": short_name(info.home.name),
                    "away_short": short_name(info.away.name),
                    "stadium": info.stadium,
                }
            )
        return result

    async def evict_absent_replay(client_ip: str | None = None) -> bool:
        """Free the slot of the replay whose viewer has been gone longest, if there is one.

        Only a replay that is paused with no stream connected (its page was hidden or lost) is
        a candidate: it costs a slot but nobody is watching. Its viewer gets a 404 on return,
        and the page shows "This replay has ended". With `client_ip`, only that address's
        replays are candidates (for the per-address cap).
        """
        absent = [
            item
            for item in sessions.values()
            if not item.stopped
            and (client_ip is None or item.client_ip == client_ip)
            and not item.stream_open
            and item.away_since is not None
            and item.replay.state == "paused"
        ]
        if not absent:
            return False
        oldest = min(absent, key=lambda item: item.away_since or 0.0)
        logger.info(
            "replay_evicted replay_id=%s away_seconds=%.0f",
            oldest.replay_id,
            time.monotonic() - (oldest.away_since or 0.0),
        )
        await _stop_session(oldest)
        return True

    async def prepare_replay(events: list[Event], speed: int) -> Replay:
        """Reserve a slot; when the app is full, take one from a viewer who has gone."""
        try:
            return manager.prepare(events, speed)
        except RuntimeError as exc:
            if "Maximum concurrent replays" not in str(exc) or not await evict_absent_replay():
                raise
        return manager.prepare(events, speed)

    @app.post("/api/matches/{match_id}/start")
    async def start_match(
        match_id: str,
        request: Request,
        speed: int = 1,
        mode: ProfileMode = "casual",
        team: ProfileTeam = "neutral",
    ) -> dict[str, Any]:
        warmup = app.state.warmup
        # Cached mode, audio off, for either reason: today's spending cap, or a live model
        # that failed its warm-up and is still being retried.
        spending_limited = guard.tripped
        limited = spending_limited or warm_up_failed()
        if warmup["status"] != "ready" and not limited:
            raise HTTPException(
                status_code=503,
                detail="Pundit warming up; Start will be available when it is ready.",
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
        def running_here() -> int:
            return sum(
                1
                for item in sessions.values()
                if item.client_ip == client_ip
                and not item.stopped
                and item.replay.state not in {"finished", "stopped"}
            )

        # At the cap, a replay from this address whose viewer has gone (a locked phone) gives
        # way before anyone present is refused.
        if running_here() >= MAX_REPLAYS_PER_ADDRESS:
            await evict_absent_replay(client_ip)
        if running_here() >= MAX_REPLAYS_PER_ADDRESS:
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
            replay = await prepare_replay(events, speed)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            if "Maximum concurrent replays" in str(exc):
                # Every slot is taken: a friendly "busy" the page shows with a retry button.
                logger.info(
                    "replay_refused_busy slots=%s", configured_settings.max_concurrent_replays
                )
                raise HTTPException(
                    status_code=503,
                    detail={"code": "busy", "message": BUSY_MESSAGE},
                    headers={"Retry-After": "60"},
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
            colour_gate=ColourLineGate(),
            pipeline=cached_pipeline() if limited else None,
            audio_enabled=not limited,
            profile=ViewerProfile(mode=mode, team=team),
        )
        if limited:
            logger.warning(
                "replay_started_limited replay_id=%s mode=cached audio=off reason=%s",
                replay_id,
                "spending_cap" if spending_limited else "warm_up_failed",
            )
        session.dispatcher = LiveDispatcher(
            session_pipeline(session),
            info,
            events,
            configured_settings,
            delay=delay,
            on_outcome=lambda outcome: publish_outcome(session, outcome),
            on_line_ready=lambda outcome: prepare_line_audio(session, outcome),
            profile=lambda: session.profile,
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
            "notice": (
                CACHED_NOTICE if spending_limited else WARMUP_NOTICE if limited else None
            ),
            "profile": session.profile.as_dict(),
        }

    async def set_paused(session: ReplaySession, paused: bool) -> str:
        """Pause or resume the match clock and the viewer's delayed stream together."""
        state = await session.replay.control("pause" if paused else "resume")
        # The viewer's delayed stream pauses with the replay, so nothing more arrives.
        if state == "paused":
            session.delay.pause()
        elif state == "running":
            session.delay.resume()
        return state

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
        state = await set_paused(session, action == "pause")
        return {"replay_id": replay_id, "state": state}

    @app.post("/api/replays/{replay_id}/profile")
    async def set_profile(
        replay_id: str,
        payload: ViewerProfileRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Change Mode or My team mid-match; the next line, answer and recap use it."""
        session = find_session(replay_id, request)
        session.profile = ViewerProfile(mode=payload.mode, team=payload.team)
        logger.info(
            "viewer_profile_changed replay_id=%s mode=%s team=%s",
            replay_id,
            payload.mode,
            payload.team,
        )
        return {"replay_id": replay_id, "profile": session.profile.as_dict()}

    @app.post("/api/replays/{replay_id}/playback-state")
    async def playback_state(
        replay_id: str,
        payload: ViewerPlaybackStateRequest,
        request: Request,
    ) -> dict[str, bool]:
        session = find_session(replay_id, request)
        if payload.revision <= session.viewer_line_state_revision:
            return {"accepted": False}
        session.viewer_line_state_revision = payload.revision
        session.colour_gate.update_viewer_queue(payload.audio_busy, time.monotonic())
        return {"accepted": True}

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
        with turn_span("question", session.info.match_id, session.profile) as span:
            span.set_attribute("app.as_of", as_of)
            span.set_attribute("app.question.characters", len(question))
            async def checking() -> None:
                # The first answer failed the evidence check; the page shows
                # "Checking the facts…" until the second attempt's answer arrives.
                await session.outbox.put(("answer_checking", {"question_id": question_id}))

            result = await pipeline.answer_question(
                session.info,
                session.events,
                question,
                as_of=as_of,
                on_retry=checking,
                profile=session.profile,
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
        question_id = payload.question_id or uuid4().hex
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
                "ready": warmup["status"] == "ready" or guard.tripped or warm_up_failed(),
                "error": warmup["error"],
                # True once today's spending cap is reached, or while a failed warm-up is
                # retried: new replays are cached, audio off.
                "limited": guard.tripped or warm_up_failed(),
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

        Every message has an `id:` (its outbox position). A dropped stream pauses a running
        replay at once (a locked phone shouldn't miss the match), and the paused replay is kept
        for STREAM_PAUSED_GRACE_SECONDS. Each connection closes itself after
        STREAM_MAX_SECONDS, inside the ingress request timeout; that planned close doesn't
        pause, and the replay keeps running for STREAM_RECONNECT_GRACE_SECONDS while the
        browser reconnects.
        """
        session = find_session(replay_id, request)
        position = _resume_position(last_event_id, len(session.outbox))
        # The newest connection wins: an older one (often a dead socket nobody has noticed
        # yet) ends instead of blocking the reconnect.
        session.stream_generation += 1
        generation = session.stream_generation
        session.outbox.wake()
        session.stream_open = True
        session.away_since = None
        if session.stream_deadline_task is not None:
            session.stream_deadline_task.cancel()
            session.stream_deadline_task = None
        if position:
            logger.info("stream_resumed replay_id=%s after=%s", replay_id, position)

        async def events() -> AsyncIterator[bytes]:
            nonlocal position
            closes_at = time.monotonic() + configured_settings.stream_max_seconds
            planned_close = False
            try:
                yield f"retry: {SSE_RETRY_MILLISECONDS}\n\n".encode()
                while session.stream_generation == generation and not session.outbox.closed:
                    remaining = closes_at - time.monotonic()
                    if remaining <= 0:
                        # Close cleanly before ingress cuts the request; the browser reconnects.
                        planned_close = True
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
                    session.away_since = time.monotonic()
                    if not planned_close and session.replay.state == "running":
                        # The viewer has gone (lock screen, app switch, lost signal): stop the
                        # match clock now rather than let it run on unseen. The page resumes
                        # it when the viewer is back.
                        await set_paused(session, True)
                        logger.info("replay_paused_on_disconnect replay_id=%s", replay_id)
                    # Paused (or finished), it makes no model or speech calls, so it can wait
                    # longer for the viewer to come back.
                    held = session.replay.state != "running"
                    session.stream_deadline_task = asyncio.create_task(
                        stop_without_stream(
                            session,
                            configured_settings.stream_paused_grace_seconds
                            if held
                            else configured_settings.stream_reconnect_grace_seconds,
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
