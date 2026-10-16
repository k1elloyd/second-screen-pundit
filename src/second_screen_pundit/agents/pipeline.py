from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from agent_framework import (
    Agent,
    AgentExecutor,
    FunctionExecutor,
    FunctionMiddleware,
    Message,
    WorkflowBuilder,
    WorkflowContext,
)
from agent_framework.exceptions import ChatClientException
from agent_framework.foundry import FoundryChatClient
from azure.core.credentials import TokenCredential
from pydantic import BaseModel, ValidationError

from second_screen_pundit.agents.cache import (
    CachedMatchOutput,
    closest_cached_question,
    load_cached_match,
)
from second_screen_pundit.agents.clients import (
    AgentRole,
    AgentTool,
    _MAX_TOKENS,
    create_foundry_agent,
    create_foundry_chat_client,
    deployment_for_role,
)
from second_screen_pundit.agents.commentator import (
    build_console_insight,
    console_tool_results,
    live_line_id,
    score_phrase,
    team_display_name,
    validate_plain_spoken_text,
)
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.delivery import tier_for_event, tier_for_signals
from second_screen_pundit.agents.evidence import EvidenceReport, check_evidence
from second_screen_pundit.agents.fallbacks import fallback_lines
from second_screen_pundit.agents.models import (
    Claim,
    CommentaryLine,
    DirectorDecision,
    Explanation,
    Insight,
    Recap,
    RecapMoment,
)
from second_screen_pundit.agents.prompts import (
    ANALYST_INSTRUCTIONS,
    COMMENTATOR_INSTRUCTIONS,
    DIRECTOR_INSTRUCTIONS,
    EXPLAINER_INSTRUCTIONS,
    RECAP_INSTRUCTIONS,
)
from second_screen_pundit.agents.signals import (
    SIGNAL_TOOL,
    TacticalSignal,
    signal_prompt,
    signals_tool_result,
)
from second_screen_pundit.agents.tools.analytics import (
    Fact,
    ToolResult,
    is_big_chance,
    key_moments,
    match_context,
    match_state,
)
from second_screen_pundit.agents.tools.parameters import KeyMomentsParams, MatchStateParams
from second_screen_pundit.agents.tools.runtime import build_analytics_tools
from second_screen_pundit.engine.schema import Event, MatchInfo


logger = logging.getLogger(__name__)
_OutputT = TypeVar("_OutputT", bound=BaseModel)
_FOUNDRY_TOKEN_SCOPE = "https://ai.azure.com/.default"


@dataclass(frozen=True)
class AgentCallTiming:
    role: AgentRole
    attempt: int
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class LiveWindowResult:
    status: Literal["commentary", "skipped", "timed_out", "off", "cached", "rejected"]
    line: CommentaryLine | None
    insight: Insight | None
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport | None
    notice: str | None = None
    timings: tuple[AgentCallTiming, ...] = ()


@dataclass(frozen=True)
class QuestionResult:
    explanation: Explanation
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport
    attempts: int


@dataclass(frozen=True)
class RecapResult:
    recap: Recap
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport
    attempts: int


def is_must_say(event: Event, events: Sequence[Event] = ()) -> bool:
    if event.type == "shot" and any(
        candidate.type == "goal" and event.event_id in candidate.related_event_ids
        for candidate in events
    ):
        return False
    return event.type in {"goal", "card", "substitution", "formation_change"} or (
        event.type == "shot" and is_big_chance(event.xg)
    )


def _event_clock(event: Event) -> int:
    return event.minute + event.added_minute


_MUST_SAY_FALLBACK_KINDS = {
    "goal": "goal",
    "substitution": "substitution",
    "formation_change": "formation_change",
    "shot": "big_chance",
}
_SIGNAL_FALLBACK_KINDS = {
    "red_card": "signal_red_card",
    "formation_change": "signal_formation_change",
    "possession_swing": "possession_swing",
    "high_turnover_surge": "high_turnover_surge",
    "xg_surge": "xg_surge",
}


def _response_value(payload: object) -> object | None:
    response = getattr(payload, "agent_response", payload)
    return getattr(response, "value", None)


def _as_model(value: object, model: type[_OutputT]) -> _OutputT | None:
    if isinstance(value, model):
        return value
    if isinstance(value, dict):
        return model.model_validate(value)
    return None


_INVALID_OUTPUT_PREFIX = "Return a complete, concise"


def _invalid_output(model_name: str) -> str:
    return f"{_INVALID_OUTPUT_PREFIX} {model_name} matching the schema."


def _parsed(response: object | None, model: type[_OutputT]) -> _OutputT | None:
    """Read a structured response; None when it was missing, truncated or off-schema."""
    if response is None:
        return None
    try:
        return _as_model(getattr(response, "value", None), model)
    except ValidationError:
        return None


def _is_structured_output_error(error: ChatClientException) -> bool:
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, ValidationError):
            return True
        cause = cause.__cause__
    return False


def _workflow_models(result: object) -> list[BaseModel]:
    values: list[BaseModel] = []
    for name in ("get_intermediate_outputs", "get_outputs"):
        getter = getattr(result, name)
        for output in getter():
            value = _response_value(output)
            if isinstance(value, BaseModel):
                values.append(value)
    return values


def _all_event_ids(results: Sequence[ToolResult]) -> list[str]:
    return list(
        dict.fromkeys(
            event_id
            for result in results
            for fact in result.facts
            for event_id in fact.event_ids
        )
    )


def _result_fact_map(results: Sequence[ToolResult]) -> dict[str, Fact]:
    return {fact.fact_id: fact for result in results for fact in result.facts}


def _cached_tool_results(
    events: list[Event],
    *,
    as_of: int,
    fact_ids: Sequence[str],
) -> list[ToolResult]:
    visible = [event for event in events if event.seq <= as_of]
    if not visible:
        raise ValueError("Cannot validate cached output without visible events")
    minute = max(_event_clock(event) for event in visible)
    state = match_state(events, MatchStateParams(minute=minute), as_of=as_of)
    results = [state.model_copy(update={
        "facts": [
            fact.model_copy(update={"fact_id": "match_state:score"})
            if fact.label.startswith("Match-level score")
            else fact
            for fact in state.facts
        ]
    })]
    if "formation_at:home_formation" in fact_ids:
        from second_screen_pundit.agents.tools.analytics import formation_at
        from second_screen_pundit.agents.tools.parameters import FormationAtParams

        home_formation = formation_at(
            events,
            FormationAtParams(team="home", minute=minute),
            as_of=as_of,
        )
        results.append(home_formation.model_copy(update={
            "facts": [
                home_formation.facts[0].model_copy(
                    update={"fact_id": "formation_at:home_formation"}
                )
            ]
        }))
    if "formation_at:away_formation" in fact_ids:
        from second_screen_pundit.agents.tools.analytics import formation_at
        from second_screen_pundit.agents.tools.parameters import FormationAtParams

        away_formation = formation_at(
            events,
            FormationAtParams(team="away", minute=minute),
            as_of=as_of,
        )
        results.append(away_formation.model_copy(update={
            "facts": [
                away_formation.facts[0].model_copy(
                    update={"fact_id": "formation_at:away_formation"}
                )
            ]
        }))
    return results


def _deterministic_results(events: list[Event], as_of: int) -> list[ToolResult]:
    visible = [event for event in events if event.seq <= as_of]
    if not visible:
        raise ValueError("Cannot create deterministic facts without visible events")
    minute = max(_event_clock(event) for event in visible)
    return [
        match_state(events, MatchStateParams(minute=minute), as_of=as_of),
        key_moments(events, KeyMomentsParams(limit=10), as_of=as_of),
    ]


def _fallback_explanation(
    results: Sequence[ToolResult],
    *,
    notice: str,
) -> Explanation:
    facts_by_id = _result_fact_map(results)
    selected = list(facts_by_id.values())[:5]

    def display_label(fact: Fact) -> str:
        return re.sub(
            r"\s+(?:through|at)\s+\d+'$|,\s*\d+-\d+'(?:\s+vs\s+\d+-\d+')?$",
            "",
            fact.label,
        )

    claims = [
        Claim(
            text=f"{display_label(fact)}: {fact.value}{f' {fact.unit}' if fact.unit else ''}.",
            fact_ids=[fact.fact_id],
            event_ids=fact.event_ids,
        )
        for fact in selected
    ]
    return Explanation(
        answer="Live answers are unavailable; verified match facts follow.",
        claims=claims,
        cannot_tell=["The live explanation could not be verified."],
        notice=notice,
        fact_ids=[fact.fact_id for fact in selected],
        event_ids=_all_event_ids(results),
    )


def _fallback_recap(
    results: Sequence[ToolResult],
    events: list[Event],
    as_of: int,
    *,
    notice: str,
) -> Recap:
    facts = _result_fact_map(results)
    key_result = next((result for result in results if result.tool == "key_moments"), None)
    if key_result is None:
        raise ValueError("Deterministic recap requires key_moments facts")
    event_by_id = {event.event_id: event for event in events}
    moments: list[RecapMoment] = []
    for fact in key_result.facts:
        event_id = next(
            (candidate for candidate in fact.event_ids if candidate in event_by_id),
            None,
        )
        if event_id is None:
            continue
        event = event_by_id[event_id]
        if event.seq > as_of:
            continue
        moments.append(
            RecapMoment(
                minute=_event_clock(event),
                text=f"A {str(fact.value).replace('_', ' ')} changes the shape of the match.",
                event_ids=[event_id],
            )
        )
        if len(moments) == 3:
            break
    if len(moments) < 3:
        raise ValueError("The visible event log does not contain three recap moments")
    citations = list(
        dict.fromkeys(
            fact_id
            for result in results
            for fact_id in (fact.fact_id for fact in result.facts)
        )
    )
    cited_events = list(
        dict.fromkeys(
            [*_all_event_ids(results), *(event_id for moment in moments for event_id in moment.event_ids)]
        )
    )
    score = next(
        (fact for fact in facts.values() if fact.label.startswith("Match-level score")),
        None,
    )
    score_text = f" The verified score is {score.value}." if score is not None else ""
    return Recap(
        headline="Verified match recap",
        summary=f"The deterministic event log records the match's key turning points.{score_text}",
        key_moments=moments,
        audio_script="The match's key turning points are shown with their event references.",
        notice=notice,
        fact_ids=citations,
        event_ids=cited_events,
    )


class PunditPipeline:
    def __init__(
        self,
        settings: AgentSettings,
        credential: TokenCredential | None,
        *,
        cache_dir: Path = Path("data/cached"),
    ) -> None:
        self.settings = settings
        self.credential = credential
        self.cache_dir = cache_dir
        self._clients: dict[tuple[str, float], FoundryChatClient] = {}

    def _client(self, role: AgentRole, time_budget: float) -> FoundryChatClient:
        """One client per deployment and budget: a new client costs seconds on its first call."""
        if self.credential is None:
            raise RuntimeError("A TokenCredential is required for live model calls")
        endpoint, model_fast, model_deep = self.settings.require_foundry()
        deployment = deployment_for_role(role, model_fast, model_deep)
        client = self._clients.get((deployment, time_budget))
        if client is None:
            client = create_foundry_chat_client(
                endpoint=endpoint,
                deployment=deployment,
                credential=self.credential,
                time_budget_seconds=time_budget,
            )
            self._clients[(deployment, time_budget)] = client
        return client

    def _agent(
        self,
        role: AgentRole,
        instructions: str,
        *,
        tools: Sequence[AgentTool] = (),
        middleware: Sequence[FunctionMiddleware] = (),
        time_budget: float,
    ) -> Agent:
        if self.credential is None:
            raise RuntimeError("A TokenCredential is required for live model calls")
        endpoint, model_fast, model_deep = self.settings.require_foundry()
        return create_foundry_agent(
            role,
            endpoint=endpoint,
            model_fast=model_fast,
            model_deep=model_deep,
            credential=self.credential,
            instructions=instructions,
            tools=tools,
            middleware=middleware,
            time_budget_seconds=time_budget,
            reasoning_effort=(
                self.settings.commentator_reasoning_effort if role == "commentator" else None
            ),
            client=self._client(role, time_budget),
        )

    async def warm_up(self) -> dict[str, int]:
        """Fetch the Foundry token once and make one tiny call on the live fast-model client."""
        if self.credential is None:
            raise RuntimeError("A TokenCredential is required for live model calls")
        budget = self.settings.live_timeout_seconds
        started = time.perf_counter()
        await asyncio.to_thread(self.credential.get_token, _FOUNDRY_TOKEN_SCOPE)
        token_ms = round((time.perf_counter() - started) * 1000)
        agent = Agent(
            client=self._client("commentator", budget),
            instructions="Reply with the single word PONG.",
            default_options={"max_tokens": 16, "store": False},
        )
        call_started = time.perf_counter()
        response = await asyncio.wait_for(agent.run("Reply with the single word PONG."), timeout=budget)
        model_ms = round((time.perf_counter() - call_started) * 1000)
        if "PONG" not in response.text.upper():
            raise RuntimeError("Foundry warm-up did not return the expected PONG response")
        timing = {
            "token_ms": token_ms,
            "model_call_ms": model_ms,
            "total_ms": round((time.perf_counter() - started) * 1000),
        }
        logger.info(
            "warm_up token_ms=%d model_call_ms=%d total_ms=%d",
            timing["token_ms"],
            timing["model_call_ms"],
            timing["total_ms"],
        )
        return timing

    async def comment_on_window(
        self,
        info: MatchInfo,
        events: list[Event],
        window: Sequence[Event],
        *,
        as_of: int,
        signals: Sequence[TacticalSignal] = (),
        is_stale: Callable[[], bool] | None = None,
        recent_lines: Sequence[str] = (),
    ) -> LiveWindowResult:
        if not window:
            raise ValueError("A live window must contain at least one event")
        if self.settings.mode == "off":
            return LiveWindowResult("off", None, None, (), None, "Narrative mode is off.")
        if self.settings.mode == "cached":
            return self._cached_live_window(info, events, window, as_of)

        must_say = any(is_must_say(event, events) for event in window)
        mandatory = any(signal.mandatory for signal in signals)
        lane = "fast_path" if must_say else "tactical"
        deadline = self.settings.live_timeout_seconds
        accumulated_results: list[ToolResult] = []
        timings: list[AgentCallTiming] = []
        invalid_output_roles: set[AgentRole] = set()
        truncated_output_roles: set[AgentRole] = set()
        feedback = ""
        deterministic_console = None
        deterministic_insight = None
        deterministic_score_phrase = ""
        signal_prompts = [signal_prompt(signal) for signal in signals]
        visible = [event for event in events if event.seq <= as_of]
        clock = min(90, max(_event_clock(event) for event in visible))
        if must_say:
            event = next(event for event in window if is_must_say(event, events))
            console_results = console_tool_results(event, info, events)
            deterministic_console = build_console_insight(
                event,
                info,
                events,
                tool_results=console_results,
            )
            accumulated_results.extend(console_results)
            deterministic_insight = Insight(
                insight_id=deterministic_console.insight_id,
                minute=deterministic_console.minute,
                headline=deterministic_console.headline,
                importance=1,
                fact_ids=deterministic_console.fact_ids,
                event_ids=deterministic_console.event_ids,
            )
            deterministic_score_phrase = deterministic_console.score_phrase
        else:
            score_result = match_state(
                events,
                MatchStateParams(minute=clock),
                as_of=as_of,
            )
            score_fact = next(
                fact for fact in score_result.facts
                if fact.label.startswith("Match-level score")
            )
            accumulated_results.append(
                ToolResult(tool=score_result.tool, facts=[score_fact])
            )
            accumulated_results.append(match_context(events, as_of=as_of))
            if signals:
                accumulated_results.append(signals_tool_result(list(signals)))
            deterministic_score_phrase = score_phrase(info, str(score_fact.value))
        context_facts = [
            fact
            for result in accumulated_results
            if result.tool == "match_context"
            for fact in result.facts
        ]
        must_say_event = (
            next(event for event in window if is_must_say(event, events)) if must_say else None
        )
        if must_say_event is not None:
            goal_context_value = next(
                (str(fact.value) for fact in context_facts if "goal context" in fact.label),
                None,
            )
            delivery_tier = tier_for_event(
                must_say_event, goal_context=goal_context_value, clock=clock
            )
        else:
            delivery_tier = tier_for_signals([signal.kind for signal in signals])

        def stale_result() -> LiveWindowResult:
            return LiveWindowResult(
                "skipped",
                None,
                None,
                tuple(accumulated_results),
                None,
                "The tactical window became stale before commentary completed.",
                tuple(timings),
            )

        def is_tactical_stale() -> bool:
            return not must_say and is_stale is not None and is_stale()

        def fallback_plan() -> tuple[str, dict[str, object], list[str], Insight] | None:
            """The fallback kind, its placeholder values, cited facts and Insight, if any."""
            facts_by_id = {
                fact.fact_id: fact for result in accumulated_results for fact in result.facts
            }
            score_fact = next(
                fact for fact in facts_by_id.values() if fact.label.startswith("Match-level score")
            )
            values: dict[str, object] = {"score_phrase": deterministic_score_phrase}
            if deterministic_console is not None and must_say_event is not None:
                event = must_say_event
                context = deterministic_console.event_context
                detail = context.get("detail", {})
                if not isinstance(detail, dict):
                    raise RuntimeError("Must-say event detail is not a mapping")
                descriptor = next(
                    (
                        str(fact.value)
                        for fact in facts_by_id.values()
                        if "chance description" in fact.label.casefold()
                    ),
                    None,
                )
                values.update(
                    team=team_display_name(info, event.team) if event.team else None,
                    player_name=context.get("player_name"),
                    colour=detail.get("colour"),
                    player_on_name=detail.get("player_on_name"),
                    player_off_name=detail.get("player_off_name"),
                    formation=detail.get("formation"),
                    chance_descriptor=descriptor,
                    chance_descriptor_title=(
                        descriptor[:1].upper() + descriptor[1:] if descriptor else None
                    ),
                )
                if event.type == "card":
                    kind = "red_card" if detail.get("colour") == "red" else "booking"
                else:
                    kind = _MUST_SAY_FALLBACK_KINDS.get(event.type)
                if kind is None or deterministic_insight is None:
                    return None
                return kind, values, list(deterministic_console.fact_ids), deterministic_insight

            signal = next((signal for signal in signals if signal.mandatory), None) or next(
                (signal for signal in signals if signal.kind in _SIGNAL_FALLBACK_KINDS), None
            )
            if signal is None or signal.kind not in _SIGNAL_FALLBACK_KINDS:
                return None
            kind = _SIGNAL_FALLBACK_KINDS[signal.kind]
            chosen = [signal]
            reshape = next(
                (
                    other
                    for other in signals
                    if other.kind == "formation_change" and other.team == signal.team
                ),
                None,
            )
            if signal.kind == "red_card" and reshape is not None:
                kind, chosen = "signal_red_card_formation", [signal, reshape]
            values["team"] = team_display_name(info, signal.team)
            for chosen_signal in chosen:
                prefix = f"{SIGNAL_TOOL}:{chosen_signal.key}_"
                for fact in chosen_signal.facts:
                    if fact.fact_id.startswith(prefix):
                        name = fact.fact_id[len(prefix):].split("_", 1)[1]
                        values[name] = (
                            fact.spoken_value if fact.spoken_value is not None else fact.value
                        )
            fact_ids = list(
                dict.fromkeys(
                    [
                        score_fact.fact_id,
                        *(fact.fact_id for fact in context_facts),
                        *(fact.fact_id for item in chosen for fact in item.facts),
                    ]
                )
            )
            event_ids = list(
                dict.fromkeys(
                    event_id
                    for fact_id in fact_ids
                    for event_id in facts_by_id[fact_id].event_ids
                )
            )
            insight = Insight(
                insight_id=f"{info.match_id}-i{as_of:04d}-{lane}",
                minute=clock,
                headline=f"Tactical beat: {signal.kind.replace('_', ' ')}",
                importance=1,
                fact_ids=fact_ids,
                event_ids=event_ids,
            )
            return kind, values, fact_ids, insight

        def fallback_result(reason: str | None = None) -> LiveWindowResult:
            plan = fallback_plan()
            if plan is None:
                raise RuntimeError("No guaranteed live beat is available for fallback")
            kind, values, fact_ids, insight = plan
            line_id = live_line_id(info.match_id, as_of, lane)
            failures: tuple[str, ...] = ()
            for text in fallback_lines(kind, values, line_id=line_id):
                line = CommentaryLine(
                    line_id=line_id,
                    text=text,
                    insight_id=insight.insight_id,
                    fact_ids=list(dict.fromkeys(fact_ids)),
                    event_ids=insight.event_ids,
                    tone="measured",
                    source="fallback",
                    delivery_tier=delivery_tier,
                )
                report = self._check_line(
                    line,
                    insight,
                    info=info,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    lane=lane,
                    allow_fallback=True,
                )
                if report.passed:
                    return LiveWindowResult(
                        "commentary",
                        line,
                        insight,
                        tuple(accumulated_results),
                        report,
                        reason or "Agent output failed; deterministic fallback was used.",
                        tuple(timings),
                    )
                failures = report.failures
            raise RuntimeError(
                "Deterministic live fallback failed evidence validation: " + "; ".join(failures)
            )

        async def run_agent(
            role: AgentRole,
            agent: Agent,
            prompt: str,
            attempt: int,
        ) -> object:
            started = time.perf_counter()
            response: object | None = None
            try:
                response = await agent.run(prompt)
                return response
            finally:
                finish_reason = getattr(response, "finish_reason", None)
                finish_reason = getattr(finish_reason, "value", finish_reason)
                normalized_finish_reason = (
                    str(finish_reason).casefold()
                    if finish_reason is not None
                    else None
                )
                if normalized_finish_reason in {"length", "max_tokens"}:
                    truncated_output_roles.add(role)
                    logger.warning(
                        "agent_output_truncated role=%s as_of=%d attempt=%d max_tokens=%d",
                        role,
                        as_of,
                        attempt,
                        _MAX_TOKENS[role],
                    )
                usage = getattr(response, "usage_details", None) or {}
                timing = AgentCallTiming(
                    role=role,
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    input_tokens=usage.get("input_token_count"),
                    output_tokens=usage.get("output_token_count"),
                    reasoning_tokens=usage.get("reasoning_output_token_count"),
                )
                timings.append(timing)
                logger.info(
                    "agent_call role=%s as_of=%d attempt=%d duration_ms=%d "
                    "input_tokens=%s output_tokens=%s reasoning_tokens=%s finish_reason=%s",
                    role,
                    as_of,
                    attempt,
                    timing.duration_ms,
                    timing.input_tokens,
                    timing.output_tokens,
                    timing.reasoning_tokens,
                    normalized_finish_reason,
                )

        async def call(
            role: AgentRole,
            agent: Agent,
            prompt: str,
            attempt: int,
        ) -> object | None:
            """Run one agent; None means its structured output was truncated or invalid."""
            try:
                return await run_agent(role, agent, prompt, attempt)
            except ChatClientException as exc:
                if not _is_structured_output_error(exc):
                    raise
                invalid_output_roles.add(role)
                return None

        async def attempts() -> LiveWindowResult:
            nonlocal feedback
            director_passed = must_say or mandatory
            insight = deterministic_insight
            insight_facts: list[dict[str, object]] = (
                deterministic_console.facts if deterministic_console is not None else []
            )
            event_contexts: list[dict[str, object]] = (
                [deterministic_console.event_context] if deterministic_console is not None else []
            )
            for attempt in (1, 2):
                prompt_data = {
                    "match": {
                        "id": info.match_id,
                        "home": info.home.name,
                        "away": info.away.name,
                    },
                    "must_say": must_say,
                    "minute": clock,
                    "score_phrase": deterministic_score_phrase,
                    "signals": signal_prompts,
                    "retry_feedback": feedback,
                    "required_line_id": live_line_id(info.match_id, as_of, lane),
                }
                if not director_passed:
                    director = self._agent(
                        "director",
                        DIRECTOR_INSTRUCTIONS,
                        time_budget=deadline,
                    )
                    director_response = await call(
                        "director",
                        director,
                        json.dumps(prompt_data),
                        attempt,
                    )
                    if is_tactical_stale():
                        return stale_result()
                    director_decision = _parsed(director_response, DirectorDecision)
                    if director_decision is None:
                        invalid_output_roles.add("director")
                        feedback = _invalid_output("DirectorDecision")
                        continue
                    decision_report = check_evidence(
                        director_decision,
                        events=events,
                        as_of=as_of,
                        tool_results=[],
                        info=info,
                        input_event_ids=[
                            event_id
                            for signal in signals
                            for event_id in signal.event_ids
                        ],
                    )
                    if not decision_report.passed:
                        feedback = "; ".join(decision_report.failures)
                        continue
                    if not (
                        director_decision.should_comment
                        and director_decision.importance >= self.settings.importance_threshold
                    ):
                        return LiveWindowResult(
                            "skipped",
                            None,
                            None,
                            tuple(accumulated_results),
                            decision_report,
                            timings=tuple(timings),
                        )
                    director_passed = True
                if insight is None:
                    tools, capture = build_analytics_tools(events, as_of=as_of)
                    analyst = self._agent(
                        "analyst",
                        ANALYST_INSTRUCTIONS,
                        tools=tools,
                        middleware=[capture],
                        time_budget=deadline,
                    )
                    try:
                        analyst_response = await call(
                            "analyst",
                            analyst,
                            json.dumps(prompt_data),
                            attempt,
                        )
                    finally:
                        accumulated_results.extend(capture.results)
                    if is_tactical_stale():
                        return stale_result()
                    candidate = _parsed(analyst_response, Insight)
                    if candidate is None:
                        invalid_output_roles.add("analyst")
                        feedback = _invalid_output("Insight")
                        continue
                    score_fact = next(
                        fact
                        for result in accumulated_results
                        for fact in result.facts
                        if fact.label.startswith("Match-level score")
                    )
                    candidate = candidate.model_copy(
                        update={
                            "fact_ids": list(
                                dict.fromkeys(
                                    [
                                        *candidate.fact_ids,
                                        score_fact.fact_id,
                                        *(fact.fact_id for fact in context_facts),
                                    ]
                                )
                            ),
                            "event_ids": list(
                                dict.fromkeys([*candidate.event_ids, *score_fact.event_ids])
                            ),
                        }
                    )
                    insight_report = check_evidence(
                        candidate,
                        events=events,
                        as_of=as_of,
                        tool_results=accumulated_results,
                        info=info,
                    )
                    unsupported = set(candidate.event_ids) - set(
                        _all_event_ids(accumulated_results)
                    )
                    if insight_report.passed and unsupported:
                        insight_report = EvidenceReport(
                            False,
                            tuple(
                                f"event_id {event_id} was not returned by an analytics tool"
                                for event_id in sorted(unsupported)
                            ),
                        )
                    if not insight_report.passed:
                        feedback = "; ".join(insight_report.failures)
                        continue
                    insight = candidate
                    cited_facts = {
                        fact.fact_id: fact
                        for result in accumulated_results
                        for fact in result.facts
                    }
                    insight_facts = [
                        {
                            "fact_id": fact.fact_id,
                            "label": fact.label,
                            "value": fact.value,
                            "unit": fact.unit,
                            "spoken_value": fact.spoken_value,
                        }
                        for fact_id in insight.fact_ids
                        if (fact := cited_facts.get(fact_id)) is not None
                    ]
                    event_contexts = []

                if insight is None:
                    raise RuntimeError("Live window did not produce a deterministic or model insight")
                commentator = self._agent(
                    "commentator",
                    COMMENTATOR_INSTRUCTIONS,
                    time_budget=deadline,
                )
                commentator_prompt = json.dumps(
                    {
                        **{
                            key: value
                            for key, value in prompt_data.items()
                            if key != "signals"
                        },
                        "events": event_contexts,
                        "insight": insight.model_dump(mode="json"),
                        "facts": insight_facts,
                        "recent_lines": list(recent_lines)[-3:],
                        "required_delivery_tier": delivery_tier,
                    }
                )
                commentator_response = await call(
                    "commentator",
                    commentator,
                    commentator_prompt,
                    attempt,
                )
                if is_tactical_stale():
                    return stale_result()
                line = _parsed(commentator_response, CommentaryLine)
                if line is None:
                    invalid_output_roles.add("commentator")
                    feedback = _invalid_output("CommentaryLine")
                    continue
                line = line.model_copy(update={"delivery_tier": delivery_tier})
                report = self._check_line(
                    line,
                    insight,
                    info=info,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    lane=lane,
                )
                if report.passed:
                    return LiveWindowResult(
                        "commentary",
                        line,
                        insight,
                        tuple(accumulated_results),
                        report,
                        timings=tuple(timings),
                    )
                feedback = "; ".join(report.failures)
            roles = ", ".join(
                f"{role}{' (finish_reason=length)' if role in truncated_output_roles else ''}"
                for role in sorted(invalid_output_roles)
            )
            result = LiveWindowResult(
                "rejected",
                None,
                None,
                tuple(accumulated_results),
                EvidenceReport(False, (feedback,)),
                (
                    f"Structured output failed on both attempts for {roles}."
                    if invalid_output_roles
                    else "Commentary was dropped after two failed evidence checks."
                ),
                tuple(timings),
            )
            if (must_say or mandatory or director_passed) and fallback_plan() is not None:
                if invalid_output_roles:
                    failed_roles = ", ".join(
                        f"{role}{' (finish_reason=length)' if role in truncated_output_roles else ''}"
                        for role in sorted(invalid_output_roles)
                    )
                    return fallback_result(
                        f"Structured output failed on both attempts for {failed_roles}; "
                        "deterministic fallback was used."
                    )
                return fallback_result(
                    "Evidence check failed twice; deterministic fallback was used."
                )
            return result

        async def route_by_must_say(
            fast_path: bool,
            context: WorkflowContext[bool],
        ) -> None:
            await context.send_message(fast_path)

        async def run_fast_route(
            _: bool,
            context: WorkflowContext[LiveWindowResult],
        ) -> None:
            await context.yield_output(await attempts())

        async def run_tactical_route(
            _: bool,
            context: WorkflowContext[LiveWindowResult],
        ) -> None:
            await context.yield_output(await attempts())

        route_executor = FunctionExecutor(
            route_by_must_say,
            id="live_path_router",
            input=bool,
            output=bool,
        )
        fast_executor = FunctionExecutor(
            run_fast_route,
            id="must_say_fast_path",
            input=bool,
            workflow_output=LiveWindowResult,
        )
        tactical_executor = FunctionExecutor(
            run_tactical_route,
            id="tactical_path",
            input=bool,
            workflow_output=LiveWindowResult,
        )
        workflow = (
            WorkflowBuilder(
                start_executor=route_executor,
                output_from=[fast_executor, tactical_executor],
            )
            .add_edge(route_executor, fast_executor, condition=lambda fast_path: fast_path)
            .add_edge(
                route_executor,
                tactical_executor,
                condition=lambda fast_path: not fast_path,
            )
            .build()
        )
        try:
            run_result = await asyncio.wait_for(workflow.run(must_say), timeout=deadline)
            result = next(
                (
                    output
                    for output in run_result.get_outputs()
                    if isinstance(output, LiveWindowResult)
                ),
                None,
            )
            if result is None:
                raise RuntimeError("Live workflow returned no window result")
            return result
        except TimeoutError:
            if is_tactical_stale():
                return stale_result()
            if must_say or mandatory:
                return fallback_result(
                    "Live window exceeded its time budget; deterministic fallback was used."
                )
            return LiveWindowResult(
                "timed_out",
                None,
                None,
                tuple(accumulated_results),
                None,
                "Live commentary was dropped because its time budget expired.",
                tuple(timings),
            )

    def _check_line(
        self,
        line: CommentaryLine,
        insight: Insight,
        *,
        info: MatchInfo,
        events: list[Event],
        as_of: int,
        tool_results: list[ToolResult],
        lane: str,
        allow_fallback: bool = False,
    ) -> EvidenceReport:
        report = check_evidence(
            insight, events=events, as_of=as_of, tool_results=tool_results, info=info
        )
        if not report.passed:
            return report
        try:
            validate_plain_spoken_text(line.text)
        except ValueError as exc:
            return EvidenceReport(False, (str(exc),))
        if line.line_id != live_line_id(info.match_id, as_of, lane):
            return EvidenceReport(
                False,
                ("CommentaryLine.line_id does not match the dispatched event sequence",),
            )
        if line.source != "agent" and not (allow_fallback and line.source == "fallback"):
            return EvidenceReport(
                False,
                ("CommentaryLine.source must be agent for model output",),
            )
        if line.insight_id != insight.insight_id:
            return EvidenceReport(
                False, ("CommentaryLine.insight_id does not match the supplied Insight",)
            )
        if not set(line.fact_ids).issubset(insight.fact_ids):
            return EvidenceReport(False, ("CommentaryLine cites a fact that was not in the Insight",))
        if not set(line.event_ids).issubset(insight.event_ids):
            return EvidenceReport(
                False, ("CommentaryLine cites an event that was not in the Insight",)
            )
        if not line.event_ids:
            return EvidenceReport(False, ("CommentaryLine must cite an event",))
        return check_evidence(
            line,
            events=events,
            as_of=as_of,
            tool_results=tool_results,
            info=info,
            input_event_ids=insight.event_ids,
        )

    def _cached_live_window(
        self,
        info: MatchInfo,
        events: list[Event],
        window: Sequence[Event],
        as_of: int,
    ) -> LiveWindowResult:
        cached = load_cached_match(info.match_id, cache_dir=self.cache_dir)
        window_ids = {event.event_id for event in window}
        event_by_id = {event.event_id: event for event in events}
        line = next(
            (
                candidate
                for candidate in cached.commentary
                if window_ids.intersection(candidate.event_ids)
                and all(
                    event_id in event_by_id and event_by_id[event_id].seq <= as_of
                    for event_id in candidate.event_ids
                )
            ),
            None,
        )
        if line is None:
            return LiveWindowResult(
                "cached",
                None,
                None,
                (),
                None,
                "No pre-generated line covers this event window.",
            )
        results = _cached_tool_results(events, as_of=as_of, fact_ids=line.fact_ids)
        report = check_evidence(
            line,
            events=events,
            as_of=as_of,
            tool_results=results,
            info=info,
            input_event_ids=line.event_ids,
        )
        if not report.passed:
            return LiveWindowResult(
                "rejected",
                None,
                None,
                tuple(results),
                report,
                "The cached line failed its evidence check.",
            )
        return LiveWindowResult("cached", line, None, tuple(results), report)

    async def answer_question(
        self,
        info: MatchInfo,
        events: list[Event],
        question: str,
        *,
        as_of: int,
    ) -> QuestionResult:
        if len(question) > 300:
            raise ValueError("Question must not exceed 300 characters")
        if self.settings.mode == "cached":
            return self._cached_question(info, events, question, as_of)
        if self.settings.mode == "off":
            fallback = _deterministic_results(events, as_of)
            return self._fallback_question(
                info,
                events,
                as_of,
                fallback,
                "Narrative mode is off; showing verified facts only.",
                0,
            )

        deadline = self.settings.explainer_timeout_seconds
        accumulated_results: list[ToolResult] = []
        feedback = ""
        last_failure: str | None = None

        async def attempts() -> QuestionResult | None:
            nonlocal feedback, last_failure
            for attempt in range(2):
                capture = None
                try:
                    tools, capture = build_analytics_tools(events, as_of=as_of)
                    explainer = self._agent(
                        "explainer",
                        EXPLAINER_INSTRUCTIONS,
                        tools=tools,
                        middleware=[capture],
                        time_budget=deadline,
                    )
                    workflow = WorkflowBuilder(start_executor=AgentExecutor(explainer)).build()
                    prompt = json.dumps(
                        {
                            "match": {
                                "id": info.match_id,
                                "home": info.home.name,
                                "away": info.away.name,
                            },
                            "question": question,
                            "retry_feedback": feedback,
                        }
                    )
                    run_result = await workflow.run(
                        Message(role="user", contents=prompt)
                    )
                except Exception as exc:
                    last_failure = type(exc).__name__
                    feedback = (
                        f"The previous agent or tool run failed with {last_failure}; "
                        "retry with valid tool arguments."
                    )
                    continue
                finally:
                    if capture is not None:
                        accumulated_results.extend(capture.results)
                explanation = next(
                    (
                        model
                        for model in _workflow_models(run_result)
                        if isinstance(model, Explanation)
                    ),
                    None,
                )
                if explanation is not None:
                    report = check_evidence(
                        explanation,
                        events=events,
                        as_of=as_of,
                        tool_results=accumulated_results,
                        info=info,
                    )
                    if report.passed:
                        return QuestionResult(
                            explanation,
                            tuple(accumulated_results),
                            report,
                            attempt + 1,
                        )
                    feedback = "; ".join(report.failures)
                else:
                    feedback = "The workflow did not return a structured Explanation."
            return None

        try:
            result = await asyncio.wait_for(attempts(), timeout=deadline)
        except TimeoutError:
            deterministic = _deterministic_results(events, as_of)
            return self._fallback_question(
                info,
                events,
                as_of,
                deterministic,
                "The question exceeded its time budget; verified facts are shown instead.",
                2,
            )
        if result is not None:
            return result

        deterministic = _deterministic_results(events, as_of)
        return self._fallback_question(
            info,
            events,
            as_of,
            deterministic,
            (
                f"The live explanation failed ({last_failure}); verified facts are shown instead."
                if last_failure is not None
                else "The answer failed evidence checks; verified facts are shown instead."
            ),
            2,
        )

    def _fallback_question(
        self,
        info: MatchInfo,
        events: list[Event],
        as_of: int,
        results: Sequence[ToolResult],
        notice: str,
        attempts: int,
    ) -> QuestionResult:
        explanation = _fallback_explanation(results, notice=notice)
        report = check_evidence(
            explanation,
            events=events,
            as_of=as_of,
            tool_results=list(results),
            info=info,
        )
        if not report.passed:
            raise ValueError(
                "Deterministic question fallback failed evidence validation: "
                + "; ".join(report.failures)
            )
        return QuestionResult(explanation, tuple(results), report, attempts)

    def _cached_question(
        self,
        info: MatchInfo,
        events: list[Event],
        question: str,
        as_of: int,
    ) -> QuestionResult:
        cached = load_cached_match(info.match_id, cache_dir=self.cache_dir)
        match = closest_cached_question(cached, question)
        if match is None:
            results = _deterministic_results(events, as_of)
            return self._fallback_question(
                info,
                events,
                as_of,
                results,
                "Live answers are unavailable; showing verified facts only.",
                0,
            )
        explanation = Explanation(
            answer=match.answer,
            claims=[
                Claim(text=match.answer, fact_ids=match.fact_ids, event_ids=match.event_ids)
            ],
            fact_ids=match.fact_ids,
            event_ids=match.event_ids,
            notice="Pre-generated cached answer.",
        )
        results = _cached_tool_results(events, as_of=as_of, fact_ids=match.fact_ids)
        report = check_evidence(
            explanation,
            events=events,
            as_of=as_of,
            tool_results=results,
            info=info,
            input_event_ids=match.event_ids,
        )
        if not report.passed:
            fallback_results = _deterministic_results(events, as_of)
            return self._fallback_question(
                info,
                events,
                as_of,
                fallback_results,
                "The cached answer is not valid at this replay point; verified facts are shown.",
                0,
            )
        return QuestionResult(explanation, tuple(results), report, 0)

    async def produce_recap(
        self,
        info: MatchInfo,
        events: list[Event],
        *,
        as_of: int,
    ) -> RecapResult:
        if self.settings.mode == "cached":
            return self._cached_recap(info, events, as_of)
        deterministic = _deterministic_results(events, as_of)
        if self.settings.mode == "off":
            return self._fallback_recap_result(
                info,
                events,
                as_of,
                deterministic,
                "Narrative mode is off; showing a deterministic recap.",
                0,
            )

        deadline = self.settings.recap_timeout_seconds
        accumulated_results: list[ToolResult] = []
        feedback = ""
        last_failure: str | None = None

        async def attempts() -> RecapResult | None:
            nonlocal feedback, last_failure
            for attempt in range(2):
                capture = None
                try:
                    tools, capture = build_analytics_tools(events, as_of=as_of)
                    producer = self._agent(
                        "recap",
                        RECAP_INSTRUCTIONS,
                        tools=tools,
                        middleware=[capture],
                        time_budget=deadline,
                    )
                    workflow = WorkflowBuilder(start_executor=AgentExecutor(producer)).build()
                    prompt = json.dumps(
                        {
                            "match": {
                                "id": info.match_id,
                                "home": info.home.name,
                                "away": info.away.name,
                            },
                            "request": "Produce the full-time recap using analytics tools.",
                            "retry_feedback": feedback,
                        }
                    )
                    run_result = await workflow.run(
                        Message(role="user", contents=prompt)
                    )
                except Exception as exc:
                    last_failure = type(exc).__name__
                    feedback = (
                        f"The previous agent or tool run failed with {last_failure}; "
                        "retry with valid tool arguments."
                    )
                    continue
                finally:
                    if capture is not None:
                        accumulated_results.extend(capture.results)
                recap = next(
                    (
                        model
                        for model in _workflow_models(run_result)
                        if isinstance(model, Recap)
                    ),
                    None,
                )
                if recap is None:
                    feedback = "The workflow did not return a structured Recap."
                    continue
                report = check_evidence(
                    recap,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    info=info,
                )
                if report.passed:
                    return RecapResult(recap, tuple(accumulated_results), report, attempt + 1)
                feedback = "; ".join(report.failures)
            return None

        try:
            result = await asyncio.wait_for(attempts(), timeout=deadline)
        except TimeoutError:
            return self._fallback_recap_result(
                info,
                events,
                as_of,
                deterministic,
                "The recap exceeded its time budget; verified facts are shown instead.",
                2,
            )
        if result is not None:
            return result
        return self._fallback_recap_result(
            info,
            events,
            as_of,
            deterministic,
            (
                f"The recap failed ({last_failure}); verified facts are shown instead."
                if last_failure is not None
                else "The recap failed evidence checks; verified facts are shown instead."
            ),
            2,
        )

    def _fallback_recap_result(
        self,
        info: MatchInfo,
        events: list[Event],
        as_of: int,
        results: Sequence[ToolResult],
        notice: str,
        attempts: int,
    ) -> RecapResult:
        recap = _fallback_recap(results, events, as_of, notice=notice)
        report = check_evidence(
            recap,
            events=events,
            as_of=as_of,
            tool_results=list(results),
            info=info,
        )
        if not report.passed:
            raise ValueError(
                "Deterministic recap fallback failed evidence validation: "
                + "; ".join(report.failures)
            )
        return RecapResult(recap, tuple(results), report, attempts)

    def _cached_recap(
        self,
        info: MatchInfo,
        events: list[Event],
        as_of: int,
    ) -> RecapResult:
        cached: CachedMatchOutput = load_cached_match(info.match_id, cache_dir=self.cache_dir)
        recap = cached.recap
        results = _cached_tool_results(
            events,
            as_of=as_of,
            fact_ids=recap.fact_ids,
        )
        report = check_evidence(
            recap,
            events=events,
            as_of=as_of,
            tool_results=results,
            info=info,
            input_event_ids=[
                *recap.event_ids,
                *(
                    event_id
                    for moment in recap.key_moments
                    for event_id in moment.event_ids
                ),
            ],
        )
        if not report.passed:
            deterministic = _deterministic_results(events, as_of)
            return self._fallback_recap_result(
                info,
                events,
                as_of,
                deterministic,
                "The cached recap is not valid at this replay point; verified facts are shown.",
                0,
            )
        return RecapResult(recap, tuple(results), report, 0)
