from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from agent_framework import FunctionMiddleware
from azure.core.credentials import TokenCredential
from pydantic import BaseModel, ConfigDict

from second_screen_pundit.agents.clients import create_foundry_agent
from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.tools.analytics import (
    Fact,
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
    r"compare_windows|tactical_signals):[a-z0-9_:-]+\b",
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


class ConsoleInsight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    insight_id: str
    minute: int
    headline: str
    score_phrase: str
    event_context: dict[str, object]
    facts: list[dict[str, object]]
    fact_ids: list[str]
    event_ids: list[str]


def console_tool_results(
    event: Event,
    info: MatchInfo,
    events: list[Event],
) -> list[ToolResult]:
    minute = min(90, event.minute + event.added_minute)
    state = match_state(
        events,
        MatchStateParams(minute=minute),
        as_of=event.seq,
    )
    score = next(fact for fact in state.facts if fact.label.startswith("Match-level score"))
    results = [
        ToolResult(tool=state.tool, facts=[score]),
    ]
    if event.type == "goal":
        scoring_shots = [
            candidate
            for candidate in events
            if candidate.type == "shot"
            and candidate.event_id in event.related_event_ids
            and candidate.seq <= event.seq
        ]
        if scoring_shots:
            shot = max(scoring_shots, key=lambda candidate: candidate.seq)
            results.append(
                ToolResult(
                    tool="shot_summary",
                    facts=[
                        Fact(
                            fact_id=f"shot_summary:{event.seq}:scoring_shot_xg",
                            label=f"{team_display_name(info, event.team or 'home')} scoring shot xG at {minute}'",
                            value=shot.xg or 0.0,
                            unit="xG",
                            event_ids=[shot.event_id],
                        )
                    ],
                )
            )
        return results
    if event.type == "formation_change" and event.team is not None:
        results.extend([
            formation_at(
                events,
                FormationAtParams(team=event.team, minute=minute),
                as_of=event.seq,
            )
        ])
        return results
    if event.type == "card":
        results.append(
            ToolResult(
                tool=state.tool,
                facts=[
                    fact
                    for fact in state.facts
                    if fact.label.startswith(
                        f"{info.home.name} cards"
                        if event.team == "home"
                        else f"{info.away.name} cards"
                    )
                ],
            )
        )
        return results
    if event.type == "shot" and event.team is not None:
        summary = shot_summary(
            events,
            ShotSummaryParams(
                team=event.team,
                start=minute,
                end=min(91, minute + 1),
            ),
            as_of=event.seq,
        )
        results.extend([
            ToolResult(
                tool=summary.tool,
                facts=[
                    fact
                    for fact in summary.facts
                    if " xG" in fact.label or "best chance xG" in fact.label
                ],
            )
        ])
    del info
    return results


def team_display_name(info: MatchInfo, team: str) -> str:
    name = info.home.name if team == "home" else info.away.name
    return re.sub(r"\s+FC$", "", name, flags=re.IGNORECASE)


def score_phrase(info: MatchInfo, score: str) -> str:
    home_score, away_score = (int(value) for value in score.split("-", maxsplit=1))
    if home_score > away_score:
        return f"{team_display_name(info, 'home')} lead {home_score}-{away_score}"
    if away_score > home_score:
        return f"{team_display_name(info, 'away')} lead {away_score}-{home_score}"
    return f"The score is level at {home_score}-{away_score}"


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
    *,
    tool_results: Sequence[ToolResult] | None = None,
) -> ConsoleInsight:
    results = (
        console_tool_results(event, info, events)
        if tool_results is None
        else list(tool_results)
    )
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
        score_phrase=score_phrase(
            info,
            str(next(fact.value for fact in facts if fact.label.startswith("Match-level score"))),
        ),
        event_context=_event_context(event, info),
        facts=[fact.model_dump(mode="json") for fact in facts],
        fact_ids=[fact.fact_id for fact in facts],
        event_ids=event_ids,
    )


class Commentator:
    def __init__(
        self,
        endpoint: str,
        model_fast: str,
        model_deep: str,
        credential: TokenCredential,
        middleware: tuple[FunctionMiddleware, ...] = (),
    ) -> None:
        self.agent = create_foundry_agent(
            "commentator",
            endpoint=endpoint,
            model_fast=model_fast,
            model_deep=model_deep,
            credential=credential,
            instructions=(
                "You are a measured football broadcast commentator. Write in British English, "
                "avoid hype, and produce one or two short sentences of no more than 200 characters. "
                "Use only the supplied facts and events. Return references through fact_ids and "
                "event_ids only; never put citations, square brackets, event IDs, or fact IDs in "
                "text. The text must be plain spoken English with no markup. Do not state any "
                "number unless that value appears in a supplied fact. Use the supplied deterministic "
                "score phrase verbatim whenever stating the score. Refer to players by name, never "
                "by shirt number. Match the "
                "line_id and insight_id exactly."
            ),
            tools=[],
            middleware=middleware,
        )

    async def commentate(self, insight: ConsoleInsight, event: Event) -> CommentaryLine:
        prompt = (
            f"Return a CommentaryLine for this one event.\n"
            f"Required line_id: {event.match_id}-l{event.seq:03d}\n"
            f"Required insight_id: {insight.insight_id}\n"
            f"Deterministic score phrase: {insight.score_phrase}\n"
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
