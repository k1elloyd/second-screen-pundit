from __future__ import annotations

import asyncio
import re
from typing import Literal

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.core.credentials import TokenCredential
from pydantic import BaseModel, ConfigDict, Field

from second_screen_pundit.agents.tools.analytics import (
    ToolResult,
    formation_at,
    match_state,
    shot_summary,
)
from second_screen_pundit.agents.tools.parameters import FormationAtParams, MatchStateParams, ShotSummaryParams
from second_screen_pundit.engine.schema import Event, MatchInfo


_EVENT_ID_PATTERN = re.compile(r"\bm\d{2}-e\d{4,}\b", re.IGNORECASE)
_FACT_ID_PATTERN = re.compile(
    r"\b(?:match_state|possession_window|xg_timeline|shot_summary|"
    r"pressing_intensity|pass_network|formation_at|key_moments|"
    r"compare_windows):[a-z0-9_]+\b",
    re.IGNORECASE,
)


def validate_plain_spoken_text(text: str) -> None:
    if (
        "[" in text
        or "]" in text
        or _EVENT_ID_PATTERN.search(text)
        or _FACT_ID_PATTERN.search(text)
    ):
        raise ValueError(
            "CommentaryLine.text must be plain spoken English; citations belong in ID fields"
        )


class CommentaryLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_id: str
    text: str = Field(max_length=200)
    insight_id: str
    fact_ids: list[str]
    event_ids: list[str]
    tone: Literal["measured", "reflective", "urgent", "analytical"]


class ConsoleInsight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    insight_id: str
    minute: int
    headline: str
    event_context: dict[str, object]
    facts: list[dict[str, object]]
    fact_ids: list[str]
    event_ids: list[str]


def _facts_for_event(
    event: Event,
    info: MatchInfo,
    events: list[Event],
) -> list[ToolResult]:
    if event.type == "goal":
        state = match_state(
            events,
            MatchStateParams(minute=event.minute),
            as_of=event.seq,
        )
        return [
            ToolResult(tool=state.tool, facts=[fact for fact in state.facts if fact.label == "Score"])
        ]
    if event.type == "formation_change" and event.team is not None:
        return [
            formation_at(
                events,
                FormationAtParams(team=event.team, minute=event.minute),
                as_of=event.seq,
            )
        ]
    if event.type == "card":
        state = match_state(
            events,
            MatchStateParams(minute=event.minute),
            as_of=event.seq,
        )
        label = "Home cards" if event.team == "home" else "Away cards"
        return [ToolResult(tool=state.tool, facts=[fact for fact in state.facts if fact.label == label])]
    if event.type == "shot" and event.team is not None:
        summary = shot_summary(
            events,
            ShotSummaryParams(
                team=event.team,
                start=event.minute,
                end=min(91, event.minute + 1),
            ),
            as_of=event.seq,
        )
        return [
            ToolResult(
                tool=summary.tool,
                facts=[
                    fact
                    for fact in summary.facts
                    if fact.label in {"xG", "Best chance"}
                ],
            )
        ]
    del info
    return []


def _player_name(info: MatchInfo, team: str | None, player_id: str | None) -> str | None:
    if team not in {"home", "away"} or player_id is None:
        return None
    squad = info.home if team == "home" else info.away
    return next(
        (player["name"] for player in squad.players if player["id"] == player_id),
        None,
    )


def _event_context(event: Event, info: MatchInfo) -> dict[str, object]:
    context = event.model_dump(mode="json")
    player_id = context.pop("player_id")
    player_name = _player_name(info, event.team, player_id)
    if player_name is not None:
        context["player_name"] = player_name

    detail = dict(context["detail"])
    if event.type == "substitution":
        for key in ("player_off", "player_on"):
            substituted_id = detail.pop(key, None)
            detail[f"{key}_name"] = _player_name(info, event.team, substituted_id)
    context["detail"] = detail
    return context


def build_console_insight(
    event: Event,
    info: MatchInfo,
    events: list[Event],
) -> ConsoleInsight:
    results = _facts_for_event(event, info, events)
    facts = [fact for result in results for fact in result.facts]
    team_name = (
        info.home.name if event.team == "home" else info.away.name if event.team == "away" else "The match"
    )
    headline = {
        "goal": f"Goal for {team_name}",
        "card": f"Card for {team_name}",
        "formation_change": f"{team_name} change shape",
        "substitution": f"{team_name} make a substitution",
        "shot": f"Big chance for {team_name}",
    }.get(event.type, f"{team_name}: {event.type}")
    event_ids = list(
        dict.fromkeys(
            [
                event.event_id,
                *(event_id for fact in facts for event_id in fact.event_ids),
            ]
        )
    )
    return ConsoleInsight(
        insight_id=f"{event.match_id}-i{event.seq:04d}",
        minute=event.minute + event.added_minute,
        headline=headline,
        event_context=_event_context(event, info),
        facts=[fact.model_dump(mode="json") for fact in facts],
        fact_ids=[fact.fact_id for fact in facts],
        event_ids=event_ids,
    )


class Commentator:
    def __init__(
        self,
        endpoint: str,
        deployment: str,
        credential: TokenCredential,
    ) -> None:
        self.agent = Agent(
            client=FoundryChatClient(
                project_endpoint=endpoint,
                model=deployment,
                credential=credential,
            ),
            name="Commentator",
            instructions=(
                "You are a measured football broadcast commentator. Write in British English, "
                "avoid hype, and produce one or two short sentences of no more than 200 characters. "
                "Use only the supplied facts and events. Return references through fact_ids and "
                "event_ids only; never put citations, square brackets, event IDs, or fact IDs in "
                "text. The text must be plain spoken English with no markup. Do not state any "
                "number unless that value appears in a supplied fact. Refer to players by name, "
                "never by shirt number. Match the line_id and insight_id exactly."
            ),
            tools=[],
            default_options={
                "max_tokens": 256,
                "store": False,
                "response_format": CommentaryLine,
            },
        )

    async def commentate(self, insight: ConsoleInsight, event: Event) -> CommentaryLine:
        prompt = (
            f"Return a CommentaryLine for this one event.\n"
            f"Required line_id: {event.match_id}-l{event.seq:03d}\n"
            f"Required insight_id: {insight.insight_id}\n"
            f"Event: {insight.event_context}\n"
            f"Insight: {insight.model_dump_json()}"
        )
        response = await asyncio.wait_for(self.agent.run(prompt), timeout=15)
        if response.value is None:
            raise ValueError("Commentator returned no structured CommentaryLine")
        line = CommentaryLine.model_validate(response.value)
        validate_plain_spoken_text(line.text)
        if line.line_id != f"{event.match_id}-l{event.seq:03d}":
            raise ValueError("Commentator returned a line_id that does not match the event")
        if line.insight_id != insight.insight_id:
            raise ValueError("Commentator returned an insight_id that does not match its input")
        if not set(line.fact_ids).issubset(insight.fact_ids):
            raise ValueError("Commentator cited a fact that was not supplied")
        if not set(line.event_ids).issubset(insight.event_ids):
            raise ValueError("Commentator cited an event that was not supplied")
        if not line.event_ids:
            raise ValueError("Commentator must cite at least one event")
        return line
