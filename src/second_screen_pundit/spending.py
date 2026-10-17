"""A global daily cap on model calls and text-to-speech characters.

When either cap is reached, new viewers get cached mode with audio off and a short notice,
questions are answered from the cache, and no more speech is made. Nothing fails. The counts
are in memory (one replica) and reset at midnight UTC or on a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)

CACHED_NOTICE = (
    "The pundit has reached today's limit, so this match uses pre-written commentary "
    "with audio off. Live commentary is back tomorrow."
)


def _today() -> date:
    return datetime.now(UTC).date()


class SpendingGuard:
    def __init__(
        self,
        model_calls_per_day: int,
        tts_characters_per_day: int,
        *,
        today: Callable[[], date] = _today,
    ) -> None:
        if model_calls_per_day < 0 or tts_characters_per_day < 0:
            raise ValueError("Spending caps must not be negative")
        self.model_calls_per_day = model_calls_per_day
        self.tts_characters_per_day = tts_characters_per_day
        self._today = today
        self._day = today()
        self.model_calls = 0
        self.tts_characters = 0
        self._logged: set[str] = set()

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self.model_calls = 0
            self.tts_characters = 0
            self._logged.clear()

    def _trip(self, kind: str, used: int, cap: int) -> None:
        if kind not in self._logged:
            self._logged.add(kind)
            logger.warning("spending_guard_tripped kind=%s used=%d cap=%d", kind, used, cap)

    @property
    def models_exhausted(self) -> bool:
        self._roll()
        if self.model_calls >= self.model_calls_per_day:
            self._trip("model_calls", self.model_calls, self.model_calls_per_day)
            return True
        return False

    @property
    def speech_exhausted(self) -> bool:
        self._roll()
        if self.tts_characters >= self.tts_characters_per_day:
            self._trip("tts_characters", self.tts_characters, self.tts_characters_per_day)
            return True
        return False

    @property
    def tripped(self) -> bool:
        """Either cap reached: new viewers get cached mode with audio off."""
        models = self.models_exhausted
        speech = self.speech_exhausted
        return models or speech

    def record_model_call(self) -> None:
        self._roll()
        self.model_calls += 1
        if self.model_calls >= self.model_calls_per_day:
            self._trip("model_calls", self.model_calls, self.model_calls_per_day)

    def allow_speech(self, characters: int) -> bool:
        """Count `characters` of TTS if they fit in today's cap; refuse them otherwise."""
        self._roll()
        if self.tts_characters + characters > self.tts_characters_per_day:
            self._trip("tts_characters", self.tts_characters, self.tts_characters_per_day)
            return False
        self.tts_characters += characters
        return True

    def status(self) -> dict[str, int | bool]:
        return {
            "tripped": self.tripped,
            "model_calls": self.model_calls,
            "model_calls_per_day": self.model_calls_per_day,
            "tts_characters": self.tts_characters,
            "tts_characters_per_day": self.tts_characters_per_day,
        }
