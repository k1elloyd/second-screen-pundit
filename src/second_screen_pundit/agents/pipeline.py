from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from agent_framework import Agent, AgentExecutor, FunctionMiddleware, Message, WorkflowBuilder
from azure.core.credentials import TokenCredential
from pydantic import BaseModel

from second_screen_pundit.agents.cache import (
    CachedMatchOutput,
    closest_cached_question,
    load_cached_match,
)
from second_screen_pundit.agents.clients import AgentRole, AgentTool, create_foundry_agent
from second_screen_pundit.agents.commentator import validate_plain_spoken_text
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.evidence import EvidenceReport, check_evidence
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
from second_screen_pundit.agents.tools.analytics import Fact, ToolResult, key_moments, match_state
from second_screen_pundit.agents.tools.parameters import KeyMomentsParams, MatchStateParams
from second_screen_pundit.agents.tools.runtime import build_analytics_tools
from second_screen_pundit.engine.schema import Event, MatchInfo


_OutputT = TypeVar("_OutputT", bound=BaseModel)


@dataclass(frozen=True)
class LiveWindowResult:
    status: Literal["commentary", "skipped", "timed_out", "off", "cached", "rejected"]
    line: CommentaryLine | None
    insight: Insight | None
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport | None
    notice: str | None = None


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


def is_must_say(event: Event) -> bool:
    return event.type in {"goal", "card", "substitution", "formation_change"} or (
        event.type == "shot" and event.xg is not None and event.xg > 0.25
    )


def _event_clock(event: Event) -> int:
    return event.minute + event.added_minute


def _response_value(payload: object) -> object | None:
    response = getattr(payload, "agent_response", payload)
    return getattr(response, "value", None)


def _as_model(value: object, model: type[_OutputT]) -> _OutputT | None:
    if isinstance(value, model):
        return value
    if isinstance(value, dict):
        return model.model_validate(value)
    return None


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
    results = [match_state(events, MatchStateParams(minute=minute), as_of=as_of)]
    if "formation_at:home_formation" in fact_ids:
        from second_screen_pundit.agents.tools.analytics import formation_at
        from second_screen_pundit.agents.tools.parameters import FormationAtParams

        results.append(
            formation_at(
                events,
                FormationAtParams(team="home", minute=minute),
                as_of=as_of,
            )
        )
    if "formation_at:away_formation" in fact_ids:
        from second_screen_pundit.agents.tools.analytics import formation_at
        from second_screen_pundit.agents.tools.parameters import FormationAtParams

        results.append(
            formation_at(
                events,
                FormationAtParams(team="away", minute=minute),
                as_of=as_of,
            )
        )
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
    claims = [
        Claim(
            text=f"{fact.label}: {fact.value}{fact.unit or ''}.",
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
    score = facts.get("match_state:score")
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
        )

    async def comment_on_window(
        self,
        info: MatchInfo,
        events: list[Event],
        window: Sequence[Event],
        *,
        as_of: int,
    ) -> LiveWindowResult:
        if not window:
            raise ValueError("A live window must contain at least one event")
        if self.settings.mode == "off":
            return LiveWindowResult("off", None, None, (), None, "Narrative mode is off.")
        if self.settings.mode == "cached":
            return self._cached_live_window(info, events, window, as_of)

        must_say = any(is_must_say(event) for event in window)
        deadline = self.settings.live_timeout_seconds
        accumulated_results: list[ToolResult] = []
        feedback = ""

        async def attempts() -> LiveWindowResult:
            nonlocal feedback
            for _ in range(2):
                tools, capture = build_analytics_tools(events, as_of=as_of)
                analyst = self._agent(
                    "analyst",
                    ANALYST_INSTRUCTIONS,
                    tools=tools,
                    middleware=[capture],
                    time_budget=deadline,
                )
                commentator = self._agent(
                    "commentator",
                    COMMENTATOR_INSTRUCTIONS,
                    time_budget=deadline,
                )
                analyst_executor = AgentExecutor(analyst)
                commentator_executor = AgentExecutor(commentator)
                prompt_data = {
                    "match": {
                        "id": info.match_id,
                        "home": info.home.name,
                        "away": info.away.name,
                    },
                    "must_say": must_say,
                    "window": [event.model_dump(mode="json") for event in window],
                    "retry_feedback": feedback,
                    "required_line_id": f"{info.match_id}-l{as_of:03d}",
                }
                if must_say:
                    workflow = (
                        WorkflowBuilder(start_executor=analyst_executor)
                        .add_edge(analyst_executor, commentator_executor)
                        .build()
                    )
                else:
                    director = self._agent(
                        "director",
                        DIRECTOR_INSTRUCTIONS,
                        time_budget=deadline,
                    )
                    director_executor = AgentExecutor(director)
                    workflow = (
                        WorkflowBuilder(start_executor=director_executor)
                        .add_edge(
                            director_executor,
                            analyst_executor,
                            condition=lambda payload: self._director_forwards(payload),
                        )
                        .add_edge(analyst_executor, commentator_executor)
                        .build()
                    )
                try:
                    run_result = await workflow.run(
                        Message(role="user", contents=json.dumps(prompt_data))
                    )
                finally:
                    accumulated_results.extend(capture.results)

                models = _workflow_models(run_result)
                decision = next(
                    (model for model in models if isinstance(model, DirectorDecision)),
                    None,
                )
                if decision is not None:
                    decision_report = check_evidence(
                        decision,
                        events=events,
                        as_of=as_of,
                        tool_results=[],
                        info=info,
                    )
                    if not decision_report.passed:
                        feedback = "; ".join(decision_report.failures)
                        continue
                    if not (
                        decision.should_comment
                        and decision.importance >= self.settings.importance_threshold
                    ):
                        return LiveWindowResult(
                            "skipped",
                            None,
                            None,
                            tuple(accumulated_results),
                            decision_report,
                        )

                insight = next((model for model in models if isinstance(model, Insight)), None)
                line = next((model for model in models if isinstance(model, CommentaryLine)), None)
                if insight is None or line is None:
                    feedback = "The workflow did not return both a structured Insight and CommentaryLine."
                    continue
                report = check_evidence(
                    insight,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    info=info,
                )
                if report.passed:
                    tool_event_ids = set(_all_event_ids(accumulated_results))
                    unsupported = set(insight.event_ids) - tool_event_ids
                    if unsupported:
                        report = EvidenceReport(
                            False,
                            tuple(
                                f"event_id {event_id} was not returned by an analytics tool"
                                for event_id in sorted(unsupported)
                            ),
                        )
                if report.passed:
                    try:
                        validate_plain_spoken_text(line.text)
                    except ValueError as exc:
                        report = EvidenceReport(False, (str(exc),))
                if report.passed:
                    if line.line_id != f"{info.match_id}-l{as_of:03d}":
                        report = EvidenceReport(
                            False,
                            ("CommentaryLine.line_id does not match the dispatched event sequence",),
                        )
                    elif line.insight_id != insight.insight_id:
                        report = EvidenceReport(
                            False,
                            ("CommentaryLine.insight_id does not match the supplied Insight",),
                        )
                    elif not set(line.fact_ids).issubset(insight.fact_ids):
                        report = EvidenceReport(
                            False,
                            ("CommentaryLine cites a fact that was not in the Insight",),
                        )
                    elif not set(line.event_ids).issubset(insight.event_ids):
                        report = EvidenceReport(
                            False,
                            ("CommentaryLine cites an event that was not in the Insight",),
                        )
                    elif not line.event_ids:
                        report = EvidenceReport(False, ("CommentaryLine must cite an event",))
                    else:
                        report = check_evidence(
                            line,
                            events=events,
                            as_of=as_of,
                            tool_results=accumulated_results,
                            info=info,
                        )
                if report.passed:
                    return LiveWindowResult(
                        "commentary",
                        line,
                        insight,
                        tuple(accumulated_results),
                        report,
                    )
                feedback = "; ".join(report.failures)
            return LiveWindowResult(
                "rejected",
                None,
                None,
                tuple(accumulated_results),
                EvidenceReport(False, (feedback,)),
                "Commentary was dropped after two failed evidence checks.",
            )

        try:
            return await asyncio.wait_for(attempts(), timeout=deadline)
        except TimeoutError:
            return LiveWindowResult(
                "timed_out",
                None,
                None,
                tuple(accumulated_results),
                None,
                "Live commentary was dropped because its time budget expired.",
            )

    def _director_forwards(self, payload: object) -> bool:
        raw_value = _response_value(payload)
        decision = _as_model(raw_value, DirectorDecision)
        return bool(
            decision
            and decision.should_comment
            and decision.importance >= self.settings.importance_threshold
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

        async def attempts() -> QuestionResult | None:
            nonlocal feedback
            for attempt in range(2):
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
                try:
                    run_result = await workflow.run(
                        Message(role="user", contents=prompt)
                    )
                finally:
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
            "The answer failed evidence checks; verified facts are shown instead.",
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

        async def attempts() -> RecapResult | None:
            nonlocal feedback
            for attempt in range(2):
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
                try:
                    run_result = await workflow.run(
                        Message(role="user", contents=prompt)
                    )
                finally:
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
            "The recap failed evidence checks; verified facts are shown instead.",
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
