from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
    MAX_LINE_CHARS,
    PREVIOUS_FORMATION_SUFFIX,
    build_console_insight,
    console_tool_results,
    live_line_id,
    score_phrase,
    team_display_name,
    tidy_spoken_text,
    validate_plain_spoken_text,
)
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.content_filter import OUT_OF_SCOPE_ANSWER, question_scope_reason
from second_screen_pundit.agents.delivery import TIER_TONES, tier_for_event, tier_for_signals
from second_screen_pundit.agents.evidence import EvidenceReport, check_evidence, mentions_score
from second_screen_pundit.agents.fallbacks import fallback_lines
from second_screen_pundit.agents.models import (
    Claim,
    CommentaryLine,
    CommentaryReply,
    DirectorDecision,
    ExplainerReply,
    Explanation,
    Insight,
    Recap,
    RecapMoment,
    RecapReply,
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
    chance_summary,
    is_big_chance,
    key_moments,
    match_context,
    match_snapshot,
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
    finish_reason: str | None = None


@dataclass(frozen=True)
class LiveWindowResult:
    status: Literal["commentary", "skipped", "timed_out", "off", "cached", "rejected"]
    line: CommentaryLine | None
    insight: Insight | None
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport | None
    notice: str | None = None
    timings: tuple[AgentCallTiming, ...] = ()
    # "role: fields | reply excerpt" for each structured-output failure in this window.
    output_failures: tuple[str, ...] = ()


AnswerSource = Literal["agent", "fallback", "out_of_scope", "cached"]


@dataclass(frozen=True)
class QuestionResult:
    explanation: Explanation
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport
    attempts: int
    # Only "agent" text is model-written; the content filter runs on nothing else.
    source: AnswerSource = "fallback"
    timings: tuple[AgentCallTiming, ...] = ()


@dataclass(frozen=True)
class RecapResult:
    recap: Recap
    tool_results: tuple[ToolResult, ...]
    evidence: EvidenceReport
    attempts: int
    source: AnswerSource = "fallback"
    timings: tuple[AgentCallTiming, ...] = ()


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


# Goals and red cards always carry the score; the delivery tier already identifies them.
_SCORE_TIERS = {"goal_peak", "goal_equaliser", "goal", "red_card"}
RECENT_LINES_FOR_SCORE = 3


def score_phrase_allowed(
    phrase: str,
    recent_lines: Sequence[str],
    *,
    goal_or_red_card: bool,
) -> bool:
    """The score is said on goals and red cards, or when none of the last 3 lines said it."""
    if goal_or_red_card:
        return True
    folded = phrase.casefold()
    return not any(folded in line.casefold() for line in recent_lines[-RECENT_LINES_FOR_SCORE:])


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


_EXCERPT_CHARS = 160


def describe_validation_error(error: ValidationError, raw: object | None = None) -> str:
    """Which fields failed and a short excerpt of the reply, for the log and the gate report."""
    problems = [
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['type']}"
        for item in error.errors()
    ]
    if raw is None:
        raw = next((item.get("input") for item in error.errors() if "input" in item), None)
    excerpt = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    excerpt = " ".join(excerpt.split())
    if len(excerpt) > _EXCERPT_CHARS:
        excerpt = excerpt[:_EXCERPT_CHARS] + "..."
    return f"{'; '.join(problems[:4])} | reply: {excerpt!r}"


def _parsed(
    response: object | None,
    model: type[_OutputT],
    on_invalid: Callable[[str], None] | None = None,
) -> _OutputT | None:
    """Read a structured response; None when it was missing, truncated or off-schema."""
    if response is None:
        return None
    try:
        parsed = _as_model(getattr(response, "value", None), model)
    except ValidationError as exc:
        if on_invalid is not None:
            on_invalid(describe_validation_error(exc, getattr(response, "text", None)))
        return None
    if parsed is None and on_invalid is not None:
        text = " ".join(str(getattr(response, "text", "") or "").split())
        on_invalid(f"<root>: no structured value | reply: {text[:_EXCERPT_CHARS]!r}")
    return parsed


def _structured_output_error(error: ChatClientException) -> ValidationError | None:
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, ValidationError):
            return cause
        cause = cause.__cause__
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


def _fact_claim(fact: Fact) -> Claim:
    """A deterministic one-line statement of a fact, for the evidence list under an answer."""
    label = re.sub(
        r"\s+(?:through|at)\s+\d+'$|,\s*\d+-\d+'(?:\s+vs\s+\d+-\d+')?$",
        "",
        fact.label,
    )
    return Claim(
        text=f"{label}: {fact.value}{f' {fact.unit}' if fact.unit else ''}.",
        fact_ids=[fact.fact_id],
        event_ids=fact.event_ids,
    )


class _FactAliases:
    """Short names (F1, F2, ...) for fact IDs in a prompt, mapped back on the server.

    Real fact IDs are long hashes; copying them cost output tokens and invited typos.
    """

    def __init__(self) -> None:
        self._by_fact: dict[str, str] = {}
        self._by_alias: dict[str, str] = {}

    def alias(self, fact_id: str) -> str:
        if fact_id not in self._by_fact:
            alias = f"F{len(self._by_fact) + 1}"
            self._by_fact[fact_id] = alias
            self._by_alias[alias] = fact_id
        return self._by_fact[fact_id]

    def real(self, alias: str) -> str | None:
        return self._by_alias.get(alias.strip())

    def present(self, result: ToolResult) -> ToolResult:
        return result.model_copy(
            update={
                "facts": [
                    fact.model_copy(update={"fact_id": self.alias(fact.fact_id)})
                    for fact in result.facts
                ]
            }
        )

    def restore(self, result: ToolResult) -> ToolResult:
        return result.model_copy(
            update={
                "facts": [
                    fact.model_copy(update={"fact_id": self.real(fact.fact_id) or fact.fact_id})
                    for fact in result.facts
                ]
            }
        )


def _prompt_facts(
    results: Sequence[ToolResult],
    aliases: _FactAliases,
    *,
    moment_event_ids: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Snapshot facts and key moments as the model sees them, with aliased IDs."""
    facts: list[dict[str, object]] = []
    moments: list[dict[str, object]] = []
    for result in results:
        rows = {str(row.get("fact_id")): row for row in result.table or []}
        for fact in result.facts:
            item: dict[str, object] = {
                "id": aliases.alias(fact.fact_id),
                "label": fact.label,
                "value": fact.value,
            }
            if fact.unit:
                item["unit"] = fact.unit
            if fact.spoken_value is not None and fact.spoken_value != fact.value:
                item["spoken_value"] = fact.spoken_value
            row = rows.get(fact.fact_id)
            if row is None:
                facts.append(item)
                continue
            moment = {**item, "minute": row["minute"], "description": row["description"]}
            if moment_event_ids:
                moment["event_ids"] = fact.event_ids
            moments.append(moment)
    return facts, moments


def full_time_score_phrase(info: MatchInfo, score: str) -> str:
    """The final score, winner first: 'Merehaven Athletic win 2-1' or 'X and Y draw 1-1'."""
    home_score, away_score = (int(value) for value in score.split("-", maxsplit=1))
    if home_score > away_score:
        return f"{team_display_name(info, 'home')} win {home_score}-{away_score}"
    if away_score > home_score:
        return f"{team_display_name(info, 'away')} win {away_score}-{home_score}"
    return (
        f"{team_display_name(info, 'home')} and {team_display_name(info, 'away')} "
        f"draw {home_score}-{away_score}"
    )


def _snapshot_score(results: Sequence[ToolResult]) -> Fact:
    return next(
        fact
        for result in results
        for fact in result.facts
        if fact.label.startswith("Match-level score")
    )


def _workflow_response(result: object) -> object | None:
    """The agent response a single-agent workflow produced, or None."""
    for name in ("get_outputs", "get_intermediate_outputs"):
        getter = getattr(result, name, None)
        if getter is None:
            continue
        for output in getter():
            response = getattr(output, "agent_response", output)
            if hasattr(response, "value"):
                return response
    return None


def _finish_reason(response: object | None) -> str | None:
    reason = getattr(response, "finish_reason", None)
    reason = getattr(reason, "value", reason)
    return str(reason).casefold() if reason is not None else None


def _failure_detail(error: BaseException) -> tuple[str, bool]:
    """Model-facing retry detail for an exception, and whether it looks like a cut-off reply."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ValidationError):
            text = " ".join(str(cause).split())
            return text[:240], "EOF while parsing" in text
        cause = cause.__cause__
    return type(error).__name__, False


async def _run_single_agent(
    agent: Agent,
    prompt: str,
    *,
    role: AgentRole,
    attempt: int,
    timings: list[AgentCallTiming],
    match_id: str,
    as_of: int,
) -> object | None:
    """Run one agent as a one-executor workflow and record its timing and finish_reason."""
    workflow = WorkflowBuilder(start_executor=AgentExecutor(agent)).build()
    started = time.perf_counter()
    response: object | None = None
    finish_reason: str | None = "error"
    try:
        run_result = await workflow.run(Message(role="user", contents=prompt))
        response = _workflow_response(run_result)
        finish_reason = _finish_reason(response) or ("no_response" if response is None else None)
        return response
    except Exception as exc:
        if _failure_detail(exc)[1]:
            finish_reason = "length"
        raise
    finally:
        usage = getattr(response, "usage_details", None) or {}
        timing = AgentCallTiming(
            role=role,
            attempt=attempt,
            duration_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("input_token_count"),
            output_tokens=usage.get("output_token_count"),
            reasoning_tokens=usage.get("reasoning_output_token_count"),
            finish_reason=finish_reason,
        )
        timings.append(timing)
        logger.info(
            "agent_call role=%s match_id=%s as_of=%d attempt=%d duration_ms=%d "
            "input_tokens=%s output_tokens=%s reasoning_tokens=%s finish_reason=%s",
            role,
            match_id,
            as_of,
            attempt,
            timing.duration_ms,
            timing.input_tokens,
            timing.output_tokens,
            timing.reasoning_tokens,
            finish_reason,
        )


def _fallback_explanation(
    results: Sequence[ToolResult],
    *,
    notice: str,
) -> Explanation:
    facts_by_id = _result_fact_map(results)
    selected = list(facts_by_id.values())[:5]
    claims = [_fact_claim(fact) for fact in selected]
    return Explanation(
        answer="Live answers are unavailable; verified match facts follow.",
        claims=claims,
        cannot_tell=["The live explanation could not be verified."],
        notice=notice,
        fact_ids=[fact.fact_id for fact in selected],
        event_ids=_all_event_ids(results),
    )


def _recap_snapshot(events: list[Event], as_of: int) -> list[ToolResult]:
    """The recap's facts: the full-time snapshot with its turning points, and the chances."""
    return [
        *match_snapshot(events, as_of=as_of, moments=RECAP_MOMENTS, minor=False),
        chance_summary(events, as_of=as_of),
    ]


def _digit_ordinal(value: int) -> str:
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", "!", "?")) else ".")


def _fallback_recap(
    info: MatchInfo,
    events: list[Event],
    as_of: int,
    *,
    notice: str,
) -> tuple[Recap, list[ToolResult]]:
    """A readable recap written only from the deterministic snapshot.

    The headline is the winner-first score phrase. The summary tells the goals and red cards
    in order, from each moment's own description, then names the team with the better of the
    chances and its rounded expected goals. No xG decimal appears anywhere.
    """
    results = _recap_snapshot(events, as_of)
    phrase = full_time_score_phrase(info, str(_snapshot_score(results).value))
    moments: list[tuple[int, str, Fact]] = []
    for result in results:
        rows = {str(row.get("fact_id")): row for row in result.table or []}
        for fact in result.facts:
            row = rows.get(fact.fact_id)
            if row is not None:
                # "Consolation" was live wording; at full time a team may have come back.
                description = str(row["description"]).replace("the consolation", "a goal back")
                moments.append((int(row["minute"]), description, fact))
    reds = [moment for moment in moments if "red card at" in moment[2].label]
    goals = [moment for moment in moments if " goal at " in moment[2].label]
    # The story: every red card and the latest goals, which decide the result.
    turning = sorted(
        [*reds, *goals[-max(0, RECAP_STORY_MOMENTS - len(reds)):]] if goals else reds,
        key=lambda moment: moment[0],
    )[-RECAP_STORY_MOMENTS:]
    chosen = sorted(reds[:3] + goals[::-1][: 3 - len(reds[:3])], key=lambda moment: moment[0])
    chosen += [moment for moment in moments if moment not in chosen][: 3 - len(chosen)]
    if len(chosen) < 3:
        raise ValueError("The visible event log does not contain three recap moments")
    chosen.sort(key=lambda moment: moment[0])
    key_moments = [
        RecapMoment(minute=minute, text=_sentence(description), event_ids=fact.event_ids[:1])
        for minute, description, fact in chosen
    ]
    story = " ".join(
        f"In the {_digit_ordinal(minute)} minute, {description}."
        for minute, description, _ in (turning or chosen)
    )
    facts = _result_fact_map(results)
    chances = ""
    leader = next(
        (fact for fact in facts.values() if fact.value == "the better of the chances"), None
    )
    if leader is not None:
        team = leader.label.split(" chance quality", 1)[0]
        rounded = next(
            (
                fact
                for fact in facts.values()
                if fact.label.startswith(f"{team} expected goals, rounded")
            ),
            None,
        )
        if rounded is not None and int(rounded.value) > 0:
            chances = (
                f" {team} had the better of the chances, with about {rounded.value} "
                "expected goals."
            )
        else:
            chances = f" {team} had the better of the chances."
    summary = f"{phrase} at {info.stadium}. {story}{chances}"
    return (
        Recap(
            headline=f"{phrase} at {info.stadium}",
            summary=summary,
            key_moments=key_moments,
            audio_script=summary,
            notice=notice,
            fact_ids=list(facts),
            event_ids=list(
                dict.fromkeys(
                    [
                        *_all_event_ids(results),
                        *(event_id for moment in key_moments for event_id in moment.event_ids),
                    ]
                )
            ),
        ),
        results,
    )


RECAP_MOMENTS = 12
# The fallback recap tells at most this many goals and red cards, so it stays short.
RECAP_STORY_MOMENTS = 5

_RETRY_HINTS = (
    ("ungrounded number", "Use only numbers that are fact values; give a minute only for a listed key moment."),
    ("score", "State the score only as score_phrase, word for word."),
    ("formation", "Write a formation exactly as a fact value, with that team named in the sentence."),
    ("per cent", "Write each percentage with %."),
    ("fact_id", "Cite only the listed F IDs."),
    ("xG decimals", "Never write xG; say who had the better of the chances, or about N expected goals from a rounded fact."),
)


def _retry_feedback(failures: Sequence[str]) -> str:
    """The evidence failures, plus one plain instruction per kind of failure."""
    hints = [
        hint
        for key, hint in _RETRY_HINTS
        if any(key in failure for failure in failures)
    ]
    return "; ".join(failures) + (". " + " ".join(hints) if hints else "")


def _cite_used_facts(
    explanation: Explanation,
    report: EvidenceReport,
    *,
    model_fact_ids: Sequence[str],
    snapshot_fact_ids: Sequence[str],
    results: Sequence[ToolResult],
    events: list[Event],
    as_of: int,
    info: MatchInfo,
) -> tuple[Explanation, EvidenceReport]:
    """Cite only the facts the answer used, once it has passed with every snapshot fact.

    Kept: the tool-result facts the model cited from its own tool calls, and, for each number,
    score, formation or minute in the answer, the snapshot facts the evidence check matched
    to it (the model's own choice first, when it cited one of them). The claims list and event
    IDs follow the kept facts, so an answer that uses one stat cites exactly that fact.
    """
    snapshot = set(snapshot_fact_ids)
    probe = explanation.model_copy(update={"claims": [], "cannot_tell": [], "notice": None})
    probe_report = check_evidence(
        probe, events=events, as_of=as_of, tool_results=list(results), info=info
    )
    chosen = [fact_id for fact_id in model_fact_ids if fact_id not in snapshot]
    # Most specific mentions first, so a minute that several facts carry (a window such as
    # 59-75') is covered by a fact already kept for a number, not by one more fact.
    for group in sorted(probe_report.grounding, key=len):
        preferred = [fact_id for fact_id in group if fact_id in model_fact_ids]
        if preferred:
            chosen.extend(preferred)
        elif not any(fact_id in chosen for fact_id in group):
            chosen.append(group[0])
    kept = set(chosen)
    # The model's own citations first, in its order; then the ones the check matched.
    chosen = list(
        dict.fromkeys(
            [*(fact_id for fact_id in model_fact_ids if fact_id in kept), *chosen]
        )
    )
    facts_by_id = _result_fact_map(results)
    cited = [facts_by_id[fact_id] for fact_id in chosen if fact_id in facts_by_id]
    narrowed = explanation.model_copy(
        update={
            "claims": [_fact_claim(fact) for fact in cited],
            "fact_ids": [fact.fact_id for fact in cited],
            "event_ids": list(
                dict.fromkeys(event_id for fact in cited for event_id in fact.event_ids)
            ),
        }
    )
    narrowed_report = check_evidence(
        narrowed, events=events, as_of=as_of, tool_results=list(results), info=info
    )
    if not narrowed_report.passed:
        # Shouldn't happen: every kept fact grounded something. Keep the passing answer.
        logger.warning(
            "question_citation_narrowing_failed match_id=%s failures=%s",
            info.match_id,
            "; ".join(narrowed_report.failures),
        )
        return explanation, report
    return narrowed, narrowed_report


def _snapshot_fact_ids(snapshot: Sequence[ToolResult]) -> list[str]:
    """Every snapshot fact the model was given, cited for it.

    The spec grounds each number in "this run's tool results"; the snapshot is one, computed
    by the server. Models listed the facts they used unreliably (a recap quoted the shot
    counts and xG without their IDs), so the server cites them all. The number check still
    needs a matching value with the team and metric named in the same sentence. Facts from
    tool calls the model made must still be cited by the model.
    """
    return [fact.fact_id for result in snapshot for fact in result.facts]


def _recap_from_reply(
    reply: RecapReply,
    aliases: _FactAliases,
    results: Sequence[ToolResult],
    events: list[Event],
    *,
    as_of: int,
    given_fact_ids: Sequence[str],
) -> tuple[Recap | None, list[str]]:
    """Map fact aliases back and check every ID against the facts and the released events.

    An ID in the wrong list (an event ID among fact_ids, or an F ID among event_ids) is moved
    to the right one; an ID that is neither gives retry feedback naming it.
    """
    facts_by_id = _result_fact_map(results)
    released = {event.event_id for event in events if event.seq <= as_of}
    problems: list[str] = []
    fact_ids: list[str] = []
    event_ids: list[str] = []

    def place(identifier: str, field: str) -> list[str]:
        """The event IDs an identifier stands for; a fact alias is also cited as a fact."""
        fact_id = aliases.real(identifier)
        if fact_id is not None and fact_id in facts_by_id:
            fact_ids.append(fact_id)
            return list(facts_by_id[fact_id].event_ids)
        if identifier in released:
            return [identifier]
        problems.append(
            f"{field} contains {identifier!r}, which is neither a listed F ID nor a listed "
            "event ID"
        )
        return []

    for identifier in reply.fact_ids:
        event_ids.extend(place(identifier, "fact_ids"))
    for identifier in reply.event_ids:
        event_ids.extend(place(identifier, "event_ids"))
    moments: list[RecapMoment] = []
    for moment in reply.key_moments:
        moment_events = [
            event_id
            for identifier in moment.event_ids
            for event_id in place(identifier, f"key moment at {moment.minute}' event_ids")
        ]
        if moment_events:
            moments.append(
                moment.model_copy(
                    update={
                        "text": " ".join(moment.text.split()),
                        "event_ids": list(dict.fromkeys(moment_events)),
                    }
                )
            )
    if problems:
        return None, problems
    fact_ids = list(dict.fromkeys([*fact_ids, *given_fact_ids]))
    event_ids = list(
        dict.fromkeys(
            [
                *event_ids,
                *(event_id for moment in moments for event_id in moment.event_ids),
            ]
        )
    )
    try:
        recap = Recap(
            headline=" ".join(reply.headline.split()),
            summary=" ".join(reply.summary.split()),
            key_moments=moments,
            audio_script=" ".join(reply.audio_script.split()),
            fact_ids=fact_ids,
            event_ids=event_ids,
        )
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in item['loc']) or 'recap'}: {item['msg']}"
            for item in exc.errors()
        ]
    return recap, []


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
        # Told about every model call before it's made (the spending guard counts them).
        self.on_model_call: Callable[[], None] | None = None

    def _count_model_call(self) -> None:
        if self.on_model_call is not None:
            self.on_model_call()

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
        score_allowed = score_phrase_allowed(
            deterministic_score_phrase,
            recent_lines,
            goal_or_red_card=delivery_tier in _SCORE_TIERS,
        )
        output_failures: list[str] = []

        def record_invalid(role: AgentRole, attempt: int) -> Callable[[str], None]:
            def record(detail: str) -> None:
                output_failures.append(f"{role}: {detail}")
                logger.warning(
                    "agent_output_invalid role=%s as_of=%d attempt=%d %s",
                    role,
                    as_of,
                    attempt,
                    detail,
                )

            return record

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
            values: dict[str, object] = {
                "score_phrase": deterministic_score_phrase if score_allowed else None
            }
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
                    previous_formation=next(
                        (
                            str(fact.value)
                            for fact_id, fact in facts_by_id.items()
                            if fact_id.endswith(f":{PREVIOUS_FORMATION_SUFFIX}")
                        ),
                        None,
                    ),
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
                    text=tidy_spoken_text(text),
                    insight_id=insight.insight_id,
                    fact_ids=list(dict.fromkeys(fact_ids)),
                    event_ids=insight.event_ids,
                    tone=TIER_TONES[delivery_tier],
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
                    score_allowed=score_allowed,
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
                        tuple(output_failures),
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
                self._count_model_call()
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
                    finish_reason=(
                        normalized_finish_reason if response is not None else "no_response"
                    ),
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
                validation_error = _structured_output_error(exc)
                if validation_error is None:
                    raise
                invalid_output_roles.add(role)
                record_invalid(role, attempt)(describe_validation_error(validation_error))
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
                    "score_phrase": deterministic_score_phrase if score_allowed else None,
                    "signals": signal_prompts,
                    "retry_feedback": feedback,
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
                    director_decision = _parsed(
                        director_response, DirectorDecision, record_invalid("director", attempt)
                    )
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
                    candidate = _parsed(analyst_response, Insight, record_invalid("analyst", attempt))
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
                        "insight": insight.model_dump(
                            mode="json", include={"headline", "fact_ids", "event_ids"}
                        ),
                        "facts": insight_facts,
                        "recent_lines": list(recent_lines)[-3:],
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
                reply = _parsed(
                    commentator_response,
                    CommentaryReply,
                    record_invalid("commentator", attempt),
                )
                if reply is None:
                    invalid_output_roles.add("commentator")
                    feedback = _invalid_output("CommentaryReply")
                    continue
                text = tidy_spoken_text(reply.text)
                if len(text) > MAX_LINE_CHARS:
                    feedback = (
                        f"text is {len(text)} characters; the limit is {MAX_LINE_CHARS}"
                    )
                    continue
                line = CommentaryLine(
                    line_id=live_line_id(info.match_id, as_of, lane),
                    text=text,
                    insight_id=insight.insight_id,
                    fact_ids=reply.fact_ids,
                    event_ids=reply.event_ids,
                    tone=TIER_TONES[delivery_tier],
                    source="agent",
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
                    score_allowed=score_allowed,
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
            return replace(result, output_failures=tuple(output_failures))
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
                tuple(output_failures),
            )
        except Exception as exc:
            logger.warning(
                "live_window_failed match_id=%s as_of=%d lane=%s error=%s",
                info.match_id,
                as_of,
                lane,
                type(exc).__name__,
            )
            if is_tactical_stale():
                return stale_result()
            if fallback_plan() is not None:
                return fallback_result(
                    f"Live agent call failed ({type(exc).__name__}); "
                    "deterministic fallback was used."
                )
            return LiveWindowResult(
                "rejected",
                None,
                None,
                tuple(accumulated_results),
                None,
                f"Live agent call failed ({type(exc).__name__}).",
                tuple(timings),
                tuple(output_failures),
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
        score_allowed: bool = True,
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
        if not score_allowed and mentions_score(line.text):
            return EvidenceReport(
                False,
                ("score_phrase was null, so this line must not state the score",),
            )
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
        if question_scope_reason(question) is not None:
            return self.out_of_scope_question()
        if self.settings.mode == "cached":
            return self._cached_question(info, events, question, as_of)
        if self.settings.mode == "off":
            fallback = _deterministic_results(events, as_of)
            return self.fallback_question(
                info,
                events,
                as_of,
                fallback,
                "Narrative mode is off; showing verified facts only.",
                0,
            )

        deadline = self.settings.explainer_timeout_seconds
        snapshot = await asyncio.to_thread(match_snapshot, events, as_of=as_of)
        aliases = _FactAliases()
        snapshot_facts, snapshot_moments = _prompt_facts(snapshot, aliases)
        visible = [event for event in events if event.seq <= as_of]
        latest = max(visible, key=lambda event: event.seq)
        score = str(_snapshot_score(snapshot).value)
        finished = any(event.type == "full_time" for event in visible)
        prompt_base = {
            "match": {
                "home": info.home.name,
                "away": info.away.name,
                "stadium": info.stadium,
            },
            "viewer_clock": "full time" if finished else f"{latest.minute + latest.added_minute}'",
            "score_phrase": (
                full_time_score_phrase(info, score) if finished else score_phrase(info, score)
            ),
            "facts": snapshot_facts,
            "key_moments": snapshot_moments,
            "question": question,
        }
        given_fact_ids = _snapshot_fact_ids(snapshot)
        accumulated_results: list[ToolResult] = list(snapshot)
        timings: list[AgentCallTiming] = []
        feedback = ""
        attempt_count = 0
        started = time.perf_counter()

        async def attempts() -> QuestionResult | None:
            nonlocal feedback, attempt_count
            for attempt in (1, 2):
                attempt_count = attempt
                capture = None
                try:
                    tools, capture = build_analytics_tools(
                        events,
                        as_of=as_of,
                        exclude={"xg_timeline"},
                        present=aliases.present,
                        restore=aliases.restore,
                    )
                    explainer = self._agent(
                        "explainer",
                        EXPLAINER_INSTRUCTIONS,
                        tools=tools,
                        middleware=[capture],
                        time_budget=deadline,
                    )
                    self._count_model_call()
                    response = await _run_single_agent(
                        explainer,
                        json.dumps({**prompt_base, "retry_feedback": feedback}),
                        role="explainer",
                        attempt=attempt,
                        timings=timings,
                        match_id=info.match_id,
                        as_of=as_of,
                    )
                except Exception as exc:
                    detail, truncated = _failure_detail(exc)
                    logger.warning(
                        "question_agent_failed match_id=%s as_of=%d attempt=%d error=%s",
                        info.match_id,
                        as_of,
                        attempt,
                        type(exc).__name__,
                    )
                    feedback = (
                        "The previous reply was cut off; keep the answer short and list each "
                        "fact ID once."
                        if truncated
                        else f"The previous reply failed ({detail}). Return answer and fact_ids "
                        "only, with valid tool arguments."
                    )
                    continue
                finally:
                    if capture is not None:
                        accumulated_results.extend(capture.results)
                reply = _parsed(response, ExplainerReply)
                if reply is None:
                    feedback = _invalid_output("ExplainerReply")
                    continue
                explanation, problems = self._explanation_from_reply(
                    reply, aliases, accumulated_results, given_fact_ids
                )
                if explanation is None:
                    feedback = "; ".join(problems)
                    continue
                report = check_evidence(
                    explanation,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    info=info,
                )
                if report.passed:
                    explanation, report = _cite_used_facts(
                        explanation,
                        report,
                        model_fact_ids=[
                            fact_id
                            for alias in reply.fact_ids
                            if (fact_id := aliases.real(alias)) is not None
                        ],
                        snapshot_fact_ids=given_fact_ids,
                        results=accumulated_results,
                        events=events,
                        as_of=as_of,
                        info=info,
                    )
                    return QuestionResult(
                        explanation,
                        tuple(accumulated_results),
                        report,
                        attempt,
                        source="agent",
                        timings=tuple(timings),
                    )
                logger.info(
                    "question_evidence_failed match_id=%s as_of=%d attempt=%d failures=%s",
                    info.match_id,
                    as_of,
                    attempt,
                    "; ".join(report.failures),
                )
                feedback = _retry_feedback(report.failures)
            return None

        try:
            result = await asyncio.wait_for(attempts(), timeout=deadline)
            notice = "The answer could not be verified; verified match facts are shown instead."
        except TimeoutError:
            result = None
            notice = "The answer took too long; verified match facts are shown instead."
        if result is None:
            fallback = self.fallback_question(
                info, events, as_of, snapshot, notice, attempt_count
            )
            result = replace(fallback, timings=tuple(timings))
        logger.info(
            "question_answered match_id=%s as_of=%d source=%s attempts=%d duration_ms=%d "
            "finish_reasons=%s",
            info.match_id,
            as_of,
            result.source,
            result.attempts,
            round((time.perf_counter() - started) * 1000),
            ",".join(str(timing.finish_reason) for timing in result.timings),
        )
        return result

    @staticmethod
    def _explanation_from_reply(
        reply: ExplainerReply,
        aliases: _FactAliases,
        results: Sequence[ToolResult],
        given_fact_ids: Sequence[str] = (),
    ) -> tuple[Explanation | None, list[str]]:
        """Map the reply's fact aliases back and derive the event IDs from the cited facts.

        ``given_fact_ids`` (every snapshot fact) are cited for the answer whether or not the
        model listed them, as the live Analyst path does for the score. The claims list
        shows only the facts the model cited.
        """
        answer = " ".join(reply.answer.split())
        problems: list[str] = []
        if not answer:
            problems.append("answer is empty")
        if len(answer.split()) > 80:
            problems.append(f"answer is {len(answer.split())} words; the limit is 80")
        facts_by_id = _result_fact_map(results)
        cited: list[Fact] = []
        for alias in dict.fromkeys(reply.fact_ids):
            fact_id = aliases.real(alias)
            if fact_id is None or fact_id not in facts_by_id:
                problems.append(f"fact_id {alias!r} is not one of the listed F IDs")
                continue
            cited.append(facts_by_id[fact_id])
        if problems:
            return None, problems
        return (
            Explanation(
                answer=answer,
                claims=[_fact_claim(fact) for fact in cited],
                fact_ids=list(
                    dict.fromkeys([*(fact.fact_id for fact in cited), *given_fact_ids])
                ),
                event_ids=list(
                    dict.fromkeys(event_id for fact in cited for event_id in fact.event_ids)
                ),
            ),
            [],
        )

    @staticmethod
    def out_of_scope_question() -> QuestionResult:
        """Betting and real-club questions get a fixed reply with no model call."""
        return QuestionResult(
            Explanation(answer=OUT_OF_SCOPE_ANSWER),
            (),
            EvidenceReport(True, ()),
            0,
            source="out_of_scope",
        )

    def fallback_question(
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
            return self.fallback_question(
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
            return self.fallback_question(
                info,
                events,
                as_of,
                fallback_results,
                "The cached answer is not valid at this replay point; verified facts are shown.",
                0,
            )
        return QuestionResult(explanation, tuple(results), report, 0, source="cached")

    async def produce_recap(
        self,
        info: MatchInfo,
        events: list[Event],
        *,
        as_of: int,
    ) -> RecapResult:
        if self.settings.mode == "cached":
            return self._cached_recap(info, events, as_of)
        if self.settings.mode == "off":
            return self._fallback_recap_result(
                info,
                events,
                as_of,
                "Narrative mode is off; showing a deterministic recap.",
                0,
            )

        deadline = self.settings.recap_timeout_seconds
        snapshot = await asyncio.to_thread(_recap_snapshot, events, as_of)
        aliases = _FactAliases()
        stats_facts, moment_facts = _prompt_facts(snapshot, aliases, moment_event_ids=True)
        # The recap never shows raw xG: the model gets the chance summary instead.
        stats_facts = [item for item in stats_facts if item.get("unit") != "xG"]
        for moment in moment_facts:
            moment["description"] = str(moment["description"]).replace(
                "the consolation", "a goal back"
            )
        score_fact = _snapshot_score(snapshot)
        recap_score_phrase = full_time_score_phrase(info, str(score_fact.value))
        prompt_base = {
            "match": {
                "home": info.home.name,
                "away": info.away.name,
                "stadium": info.stadium,
            },
            "score_phrase": recap_score_phrase,
            "facts": stats_facts,
            "key_moments": moment_facts,
        }
        given_fact_ids = _snapshot_fact_ids(snapshot)
        accumulated_results: list[ToolResult] = list(snapshot)
        timings: list[AgentCallTiming] = []
        feedback = ""
        attempt_count = 0

        async def attempts() -> RecapResult | None:
            nonlocal feedback, attempt_count
            for attempt in (1, 2):
                attempt_count = attempt
                capture = None
                try:
                    tools, capture = build_analytics_tools(
                        events,
                        as_of=as_of,
                        exclude={"xg_timeline"},
                        present=aliases.present,
                        restore=aliases.restore,
                    )
                    producer = self._agent(
                        "recap",
                        RECAP_INSTRUCTIONS,
                        tools=tools,
                        middleware=[capture],
                        time_budget=deadline,
                    )
                    self._count_model_call()
                    response = await _run_single_agent(
                        producer,
                        json.dumps({**prompt_base, "retry_feedback": feedback}),
                        role="recap",
                        attempt=attempt,
                        timings=timings,
                        match_id=info.match_id,
                        as_of=as_of,
                    )
                except Exception as exc:
                    detail, truncated = _failure_detail(exc)
                    logger.warning(
                        "recap_agent_failed match_id=%s as_of=%d attempt=%d error=%s",
                        info.match_id,
                        as_of,
                        attempt,
                        type(exc).__name__,
                    )
                    feedback = (
                        "The previous reply was cut off; keep every field within its word limit."
                        if truncated
                        else f"The previous reply failed ({detail}); retry with valid tool "
                        "arguments."
                    )
                    continue
                finally:
                    if capture is not None:
                        accumulated_results.extend(capture.results)
                reply = _parsed(response, RecapReply)
                if reply is None:
                    feedback = _invalid_output("RecapReply")
                    continue
                recap, problems = _recap_from_reply(
                    reply,
                    aliases,
                    accumulated_results,
                    events,
                    as_of=as_of,
                    given_fact_ids=given_fact_ids,
                )
                if recap is not None and recap_score_phrase.casefold() not in recap.headline.casefold():
                    recap, problems = None, [
                        f"the headline must contain score_phrase word for word: "
                        f"{recap_score_phrase!r}"
                    ]
                if recap is None:
                    feedback = "; ".join(problems)
                    continue
                report = check_evidence(
                    recap,
                    events=events,
                    as_of=as_of,
                    tool_results=accumulated_results,
                    info=info,
                )
                if report.passed:
                    return RecapResult(
                        recap,
                        tuple(accumulated_results),
                        report,
                        attempt,
                        source="agent",
                        timings=tuple(timings),
                    )
                logger.info(
                    "recap_evidence_failed match_id=%s attempt=%d failures=%s",
                    info.match_id,
                    attempt,
                    "; ".join(report.failures),
                )
                feedback = _retry_feedback(report.failures)
            return None

        try:
            result = await asyncio.wait_for(attempts(), timeout=deadline)
            notice = "The recap could not be verified; a recap from verified facts is shown."
        except TimeoutError:
            result = None
            notice = "The recap took too long; a recap from verified facts is shown."
        if result is not None:
            return result
        fallback = await asyncio.to_thread(
            self._fallback_recap_result, info, events, as_of, notice, attempt_count
        )
        return replace(fallback, timings=tuple(timings))

    def _fallback_recap_result(
        self,
        info: MatchInfo,
        events: list[Event],
        as_of: int,
        notice: str,
        attempts: int,
    ) -> RecapResult:
        recap, results = _fallback_recap(info, events, as_of, notice=notice)
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
            return self._fallback_recap_result(
                info,
                events,
                as_of,
                "The cached recap is not valid at this replay point; verified facts are shown.",
                0,
            )
        return RecapResult(recap, tuple(results), report, 0, source="cached")
