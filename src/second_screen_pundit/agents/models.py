from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class DirectorDecision(AgentOutput):
    should_comment: bool
    importance: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)


DeliveryTier = Literal[
    "goal_peak",
    "goal_equaliser",
    "goal",
    "big_chance",
    "red_card",
    "formation",
    "tactical",
    "substitution",
    "booking",
    "routine",
]


class CommentaryLine(AgentOutput):
    line_id: str
    text: str = Field(max_length=200)
    insight_id: str
    tone: Literal["measured", "reflective", "urgent", "analytical"]
    source: Literal["agent", "fallback"] = "agent"
    delivery_tier: DeliveryTier = "tactical"


class Insight(AgentOutput):
    insight_id: str
    minute: int = Field(ge=0, le=120)
    headline: str = Field(max_length=160)
    importance: float = Field(ge=0, le=1)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(max_length=500)
    fact_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class Explanation(AgentOutput):
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    cannot_tell: list[str] = Field(default_factory=list)
    notice: str | None = None

    @field_validator("answer")
    @classmethod
    def answer_is_short(cls, value: str) -> str:
        if len(value.split()) > 80:
            raise ValueError("answer must be no more than 80 words")
        return value


class RecapMoment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minute: int = Field(ge=0, le=120)
    text: str = Field(max_length=300)
    event_ids: list[str] = Field(min_length=1)


class Recap(AgentOutput):
    headline: str = Field(max_length=160)
    summary: str
    key_moments: list[RecapMoment] = Field(min_length=3, max_length=3)
    audio_script: str
    notice: str | None = None

    @field_validator("summary")
    @classmethod
    def summary_is_short(cls, value: str) -> str:
        if len(value.split()) > 200:
            raise ValueError("summary must be no more than 200 words")
        return value

    @field_validator("audio_script")
    @classmethod
    def audio_script_is_short(cls, value: str) -> str:
        if len(value.split()) > 150:
            raise ValueError("audio_script must be no more than 150 words")
        return value


class NarrativeModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["live", "cached", "off"] = "live"
