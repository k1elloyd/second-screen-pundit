from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


NarrativeMode = Literal["live", "cached", "off"]


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
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
