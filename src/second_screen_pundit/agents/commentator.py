from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from agent_framework import FunctionMiddleware
from azure.core.credentials import TokenCredential
from pydantic import BaseModel, ConfigDict

from second_screen_pundit.agents.clients import create_foundry_agent
from second_screen_pundit.agents.delivery import TIER_TONES, tier_for_event
from second_screen_pundit.agents.models import CommentaryLine, CommentaryReply
from second_screen_pundit.agents.prompts import COMMENTATOR_INSTRUCTIONS
from second_screen_pundit.agents.tools.analytics import (
    Fact,
    ToolResult,
    chance_descriptor,
    formation_at,
    match_context,
    match_state,
    shot_summary,
)
from second_screen_pundit.agents.tools.parameters import FormationAtParams, MatchStateParams, ShotSummaryParams
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name


_EVENT_ID_PATTERN = re.compile(r"\bm\d{2}-e\d{4,}\b", re.IGNORECASE)
_PLAYER_ID_PATTERN = re.compile(r"\b[a-z0-9]+-p\d{2}\b", re.IGNORECASE)
_FACT_ID_PATTERN = re.compile(
    r"\b(?:match_state|possession_window|xg_timeline|shot_summary|"
    r"pressing_intensity|pass_network|formation_at|key_moments|"
    r"compare_windows|tactical_signals|match_context):[a-z0-9_:-]+\b",
    re.IGNORECASE,
)


MAX_LINE_CHARS = 200
PREVIOUS_FORMATION_SUFFIX = "previous_formation"


def tidy_spoken_text(text: str) -> str:
    """Collapse runs of whitespace, including newlines, into single spaces."""
    return " ".join(text.split())


def validate_plain_spoken_text(text: str) -> None:
    if (
        any(bracket in text for bracket in "[]()")
        or _EVENT_ID_PATTERN.search(text)
        or _PLAYER_ID_PATTERN.search(text)
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
    team = event.team
    event_name = event.type
    if event.type == "card":
        event_name = f"{event.detail.get('colour', 'unknown')} card"
    event_team = team_name(info, team).full if team in {"home", "away"} else "Match-level"
    event_fact = Fact(
        fact_id=f"match_state:{event.seq}:event",
        label=f"{event_team} {event_name} event at {minute}'",
        value=event_name,
        unit=None,
        event_ids=[event.event_id],
    )
    results = [
        ToolResult(tool=state.tool, facts=[score]),
        ToolResult(tool=state.tool, facts=[event_fact]),
        match_context(events, as_of=event.seq, goal=event if event.type == "goal" else None),
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
            descriptor = chance_descriptor(shot.xg)
            shot_facts = [
                Fact(
                    fact_id=f"shot_summary:{event.seq}:scoring_shot",
                    label=f"{team_name(info, event.team or 'home').full} scoring shot at {minute}'",
                    value="scoring shot",
                    unit=None,
                    event_ids=[shot.event_id],
                )
            ]
            if descriptor is not None:
                shot_facts.append(
                    Fact(
                        fact_id=f"shot_summary:{event.seq}:chance_descriptor",
                        label=(
                            f"{team_name(info, event.team or 'home').full} scoring chance "
                            f"description at {minute}'"
                        ),
                        value=descriptor,
                        unit=None,
                        event_ids=[shot.event_id],
                    )
                )
            results.append(
                ToolResult(
                    tool="shot_summary",
                    facts=shot_facts,
                )
            )
        return results
    if event.type == "formation_change" and event.team is not None:
        previous = formation_at(
            events,
            FormationAtParams(team=event.team, minute=minute),
            as_of=event.seq - 1,
        ).facts[0]
        results.extend([
            formation_at(
                events,
                FormationAtParams(team=event.team, minute=minute),
                as_of=event.seq,
            ),
            ToolResult(
                tool="formation_at",
                facts=[
                    previous.model_copy(
                        update={
                            "fact_id": f"formation_at:{event.seq}:{PREVIOUS_FORMATION_SUFFIX}",
                            "label": (
                                f"{team_name(info, event.team).full} previous formation "
                                f"before {minute}'"
                            ),
                        }
                    )
                ],
            ),
        ])
        return results
    if event.type == "card":
        results.append(
            ToolResult(
                tool=state.tool,
                facts=[
                    fact
                    for fact in state.facts
                    if fact.label.startswith(f"{team_name(info, event.team or 'home').full} cards")
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
                    if "chance description" in fact.label
                ],
            )
        ])
    return results


def team_display_name(info: MatchInfo, team: str) -> str:
    """The team's full name. A line names a team in full first and by its short form after
    that; `agents/wording.name_teams` applies that to every finished line."""
    return team_name(info, team).full


def score_phrase(info: MatchInfo, score: str) -> str:
    home_score, away_score = (int(value) for value in score.split("-", maxsplit=1))
    if home_score > away_score:
        return f"{team_display_name(info, 'home')} lead {home_score}-{away_score}"
    if away_score > home_score:
        return f"{team_display_name(info, 'away')} lead {away_score}-{home_score}"
    return f"The score is level at {home_score}-{away_score}"


def live_line_id(match_id: str, as_of: int, lane: str) -> str:
    return f"{match_id}-l{as_of:03d}-{lane}"


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
        "shot": f"{team_name} chance",
    }.get(event.type, f"{team_name}: {event.type}")
    if event.type == "shot":
        descriptor = chance_descriptor(event.xg)
        headline = f"{team_name} {descriptor}" if descriptor is not None else f"{team_name} chance"
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
            instructions=COMMENTATOR_INSTRUCTIONS,
            tools=[],
            middleware=middleware,
        )

    async def commentate(self, insight: ConsoleInsight, event: Event) -> CommentaryLine:
        prompt = (
            f"Write the commentary line for this one event.\n"
            f"score_phrase: {insight.score_phrase}\n"
            f"Event: {insight.event_context}\n"
            f"Insight: {insight.model_dump_json()}"
        )
        response = await asyncio.wait_for(self.agent.run(prompt), timeout=15)
        if response.value is None:
            raise ValueError("Commentator returned no structured CommentaryReply")
        reply = CommentaryReply.model_validate(response.value)
        text = tidy_spoken_text(reply.text)
        validate_plain_spoken_text(text)
        if len(text) > MAX_LINE_CHARS:
            raise ValueError(f"Commentator text is longer than {MAX_LINE_CHARS} characters")
        if not set(reply.fact_ids).issubset(insight.fact_ids):
            raise ValueError("Commentator cited a fact that was not supplied")
        if not set(reply.event_ids).issubset(insight.event_ids):
            raise ValueError("Commentator cited an event that was not supplied")
        if not reply.event_ids:
            raise ValueError("Commentator must cite at least one event")
        tier = tier_for_event(event, goal_context=None, clock=insight.minute)
        return CommentaryLine(
            line_id=live_line_id(event.match_id, event.seq, "fast_path"),
            text=text,
            insight_id=insight.insight_id,
            fact_ids=reply.fact_ids,
            event_ids=reply.event_ids,
            tone=TIER_TONES[tier],
            delivery_tier=tier,
        )
