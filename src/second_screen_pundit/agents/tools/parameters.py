from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Team = Literal["home", "away"]
Metric = Literal[
    "possession",
    "xg",
    "shots",
    "pressing_intensity",
    "high_turnovers",
    "progressive_passes",
]


class MinuteRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0, le=90)
    end: int = Field(ge=1, le=91)

    @model_validator(mode="after")
    def validate_range(self) -> MinuteRange:
        if self.start >= self.end:
            raise ValueError("minute range must have start < end")
        return self


class MatchStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minute: int = Field(ge=0, le=90)


class PossessionWindowParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0, le=90)
    end: int = Field(ge=1, le=91)

    @model_validator(mode="after")
    def validate_window(self) -> PossessionWindowParams:
        if self.start >= self.end:
            raise ValueError("window must have start < end")
        return self


class XgTimelineParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShotSummaryParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team: Team
    start: int = Field(ge=0, le=90)
    end: int = Field(ge=1, le=91)

    @model_validator(mode="after")
    def validate_window(self) -> ShotSummaryParams:
        if self.start >= self.end:
            raise ValueError("window must have start < end")
        return self


class PressingIntensityParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team: Team
    start: int = Field(ge=0, le=90)
    end: int = Field(ge=1, le=91)

    @model_validator(mode="after")
    def validate_window(self) -> PressingIntensityParams:
        if self.start >= self.end:
            raise ValueError("window must have start < end")
        return self


class PassNetworkParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team: Team
    start: int = Field(ge=0, le=90)
    end: int = Field(ge=1, le=91)

    @model_validator(mode="after")
    def validate_window(self) -> PassNetworkParams:
        if self.start >= self.end:
            raise ValueError("window must have start < end")
        return self


class FormationAtParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team: Team
    minute: int = Field(ge=0, le=90)


class KeyMomentsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=10, ge=1, le=25)


class CompareWindowsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team: Team
    metric: Metric
    before: MinuteRange
    after: MinuteRange
