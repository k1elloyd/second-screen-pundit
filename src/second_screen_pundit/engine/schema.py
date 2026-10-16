from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TeamSide = Literal["home", "away"]
EventType = Literal[
    "kickoff",
    "pass",
    "carry",
    "shot",
    "goal",
    "save",
    "tackle",
    "interception",
    "foul",
    "card",
    "substitution",
    "formation_change",
    "half_time",
    "full_time",
]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    match_id: str = Field(pattern=r"^m\d{2}$")
    seq: int = Field(ge=1)
    period: Literal[1, 2]
    minute: int = Field(ge=0, le=90)
    added_minute: int = Field(default=0, ge=0)
    second: int = Field(ge=0, le=59)
    team: TeamSide | None
    player_id: str | None = None
    type: EventType
    x: float | None = Field(default=None, ge=0, le=100)
    y: float | None = Field(default=None, ge=0, le=100)
    end_x: float | None = Field(default=None, ge=0, le=100)
    end_y: float | None = Field(default=None, ge=0, le=100)
    outcome: Literal["success", "fail"] | None = None
    xg: float | None = Field(default=None, ge=0, le=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    related_event_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_event_shape(self) -> Event:
        expected_id = f"{self.match_id}-e{self.seq:04d}"
        if self.event_id != expected_id:
            raise ValueError(f"event_id must be {expected_id}")
        if self.period == 1 and self.minute > 45:
            raise ValueError("period 1 events cannot be after minute 45")
        if self.period == 2 and self.minute < 46:
            raise ValueError("period 2 events cannot be before minute 46")
        if self.type in {"half_time", "full_time"} and self.team is not None:
            raise ValueError(f"{self.type} events must not have a team")
        if self.type not in {"half_time", "full_time"} and self.team is None:
            raise ValueError(f"{self.type} events require a team")
        if self.type in {"kickoff", "half_time", "full_time", "formation_change"} and self.player_id is not None:
            raise ValueError(f"{self.type} events must not have a player")
        if self.type in {"pass", "carry"} and any(
            coordinate is None for coordinate in (self.x, self.y, self.end_x, self.end_y)
        ):
            raise ValueError(f"{self.type} events require start and end coordinates")
        if (
            self.type == "pass"
            and self.outcome == "success"
            and not isinstance(self.detail.get("receiver_id"), str)
        ):
            raise ValueError("successful pass events require detail.receiver_id")
        if self.type == "shot" and self.xg is None:
            raise ValueError("shot events require xg")
        if self.type != "shot" and self.xg is not None:
            raise ValueError("only shot events may include xg")
        if self.type not in {"pass", "carry"} and (
            self.end_x is not None or self.end_y is not None
        ):
            raise ValueError("only pass and carry events may include end coordinates")
        return self


class MatchSquad(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    stadium: str
    formation: str
    players: tuple[dict[str, str], ...]


class MatchInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match_id: str = Field(pattern=r"^m\d{2}$")
    seed: int
    home: MatchSquad
    away: MatchSquad
    stadium: str
    away_xg_limit: float | None = Field(default=None, ge=0, le=1)
