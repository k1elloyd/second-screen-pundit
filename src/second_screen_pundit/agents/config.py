from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


NarrativeMode = Literal["live", "cached", "off"]
CommentatorEffort = Literal["none", "low"]
# Both voices are served by the one SPEECH_ENDPOINT resource (Sweden Central).
DEFAULT_SPEECH_VOICE = "en-GB-Harry:MAI-Voice-2.1-Flash"
DEFAULT_SPEECH_FALLBACK_VOICE = "en-GB-RyanNeural"


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class AgentSettings:
    mode: NarrativeMode
    endpoint: str | None
    model_fast: str | None
    model_deep: str | None
    speech_endpoint: str | None
    speech_region: str | None
    live_timeout_seconds: float
    explainer_timeout_seconds: float
    recap_timeout_seconds: float
    importance_threshold: float
    cooldown_match_minutes: int
    broadcast_delay_seconds: float = 10
    tactical_cooldown_match_minutes: int = 10
    commentator_reasoning_effort: CommentatorEffort = "low"
    question_max_characters: int = 300
    question_audio_max_seconds: int = 15
    question_audio_max_bytes: int = 1_000_000
    questions_per_minute: int = 6
    question_rate_window_seconds: int = 60
    max_concurrent_replays: int = 6
    ask_audio_timeout_seconds: float = 30
    speech_synthesis_timeout_seconds: float = 8
    speech_voice: str = DEFAULT_SPEECH_VOICE
    # A replay whose stream hasn't connected this long after /start is stopped.
    stream_connect_deadline_seconds: float = 10
    speech_fallback_voice: str = DEFAULT_SPEECH_FALLBACK_VOICE
    # Container Apps ingress ends a request after 240 s, so each stream closes itself sooner
    # and the browser reconnects with Last-Event-ID.
    stream_max_seconds: float = 200
    # A running replay whose stream dropped is kept this long for the browser to reconnect.
    stream_reconnect_grace_seconds: float = 30
    # A paused (or finished) replay with no stream is kept this long: a locked phone or a
    # hidden tab comes back to it. Paused, it makes no model or speech calls.
    stream_paused_grace_seconds: float = 600
    # Global daily caps (UTC). When either is reached, new viewers get cached mode with audio
    # off. 0 trips the guard at once.
    model_calls_per_day: int = 900
    tts_characters_per_day: int = 45_000

    @classmethod
    def from_environment(cls) -> AgentSettings:
        raw_mode = os.getenv("NARRATIVE_MODE", "live")
        if raw_mode not in {"live", "cached", "off"}:
            raise ValueError("NARRATIVE_MODE must be live, cached, or off")
        threshold = float(os.getenv("DIRECTOR_IMPORTANCE_THRESHOLD", "0.45"))
        if not 0 <= threshold <= 1:
            raise ValueError("DIRECTOR_IMPORTANCE_THRESHOLD must be between zero and one")
        cooldown = int(os.getenv("COMMENTARY_COOLDOWN_MINUTES", "4"))
        if cooldown < 0:
            raise ValueError("COMMENTARY_COOLDOWN_MINUTES must not be negative")
        tactical_cooldown = int(os.getenv("TACTICAL_COOLDOWN_MINUTES", "10"))
        if tactical_cooldown < 0:
            raise ValueError("TACTICAL_COOLDOWN_MINUTES must not be negative")
        effort = os.getenv("COMMENTATOR_REASONING_EFFORT", "low")
        if effort not in {"none", "low"}:
            raise ValueError("COMMENTATOR_REASONING_EFFORT must be none or low")
        return cls(
            mode=raw_mode,  # type: ignore[arg-type]
            endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT"),
            model_fast=os.getenv("MODEL_FAST"),
            model_deep=os.getenv("MODEL_DEEP"),
            speech_endpoint=os.getenv("SPEECH_ENDPOINT"),
            speech_region=os.getenv("SPEECH_REGION"),
            live_timeout_seconds=_positive_float("LIVE_WINDOW_TIMEOUT_SECONDS", 15),
            explainer_timeout_seconds=_positive_float("EXPLAINER_TIMEOUT_SECONDS", 20),
            recap_timeout_seconds=_positive_float("RECAP_TIMEOUT_SECONDS", 45),
            importance_threshold=threshold,
            cooldown_match_minutes=cooldown,
            broadcast_delay_seconds=_non_negative_float("BROADCAST_DELAY_SECONDS", 10),
            tactical_cooldown_match_minutes=tactical_cooldown,
            commentator_reasoning_effort=effort,  # type: ignore[arg-type]
            question_max_characters=_positive_int("QUESTION_MAX_CHARACTERS", 300),
            question_audio_max_seconds=_positive_int("QUESTION_AUDIO_MAX_SECONDS", 15),
            question_audio_max_bytes=_positive_int("QUESTION_AUDIO_MAX_BYTES", 1_000_000),
            questions_per_minute=_positive_int("QUESTIONS_PER_MINUTE", 6),
            question_rate_window_seconds=_positive_int("QUESTION_RATE_WINDOW_SECONDS", 60),
            max_concurrent_replays=_positive_int("MAX_CONCURRENT_REPLAYS", 6),
            ask_audio_timeout_seconds=_positive_float("ASK_AUDIO_TIMEOUT_SECONDS", 30),
            speech_synthesis_timeout_seconds=_positive_float(
                "SPEECH_SYNTHESIS_TIMEOUT_SECONDS", 8
            ),
            speech_voice=os.getenv("SPEECH_VOICE", "").strip() or DEFAULT_SPEECH_VOICE,
            stream_connect_deadline_seconds=_positive_float(
                "STREAM_CONNECT_DEADLINE_SECONDS", 10
            ),
            speech_fallback_voice=(
                os.getenv("SPEECH_FALLBACK_VOICE", "").strip() or DEFAULT_SPEECH_FALLBACK_VOICE
            ),
            stream_max_seconds=_positive_float("STREAM_MAX_SECONDS", 200),
            stream_reconnect_grace_seconds=_positive_float(
                "STREAM_RECONNECT_GRACE_SECONDS", 30
            ),
            stream_paused_grace_seconds=_positive_float("STREAM_PAUSED_GRACE_SECONDS", 600),
            model_calls_per_day=_non_negative_int("MODEL_CALLS_PER_DAY", 900),
            tts_characters_per_day=_non_negative_int("TTS_CHARACTERS_PER_DAY", 45_000),
        )

    def require_foundry(self) -> tuple[str, str, str]:
        missing = [
            name
            for name, value in (
                ("FOUNDRY_PROJECT_ENDPOINT", self.endpoint),
                ("MODEL_FAST", self.model_fast),
                ("MODEL_DEEP", self.model_deep),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Set these variables in the root .env or process environment: "
                + ", ".join(missing)
            )
        return (
            self.endpoint or "",
            self.model_fast or "",
            self.model_deep or "",
        )

    def require_speech(self) -> tuple[str, str]:
        missing = [
            name
            for name, value in (
                ("SPEECH_ENDPOINT", self.speech_endpoint),
                ("SPEECH_REGION", self.speech_region),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Set these variables in the root .env or process environment: "
                + ", ".join(missing)
            )
        return self.speech_endpoint or "", self.speech_region or ""
