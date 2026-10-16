from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_framework import MiddlewareFailure

from second_screen_pundit.agents.cache import load_cached_match
from second_screen_pundit.agents.clients import create_foundry_agent
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.models import (
    Claim,
    CommentaryLine,
    DirectorDecision,
    Explanation,
    Insight,
)
from second_screen_pundit.agents.pipeline import (
    PunditPipeline,
    _cached_tool_results,
    _workflow_models,
)
from second_screen_pundit.agents.tools.analytics import (
    Fact,
    MatchStateParams,
    ShotSummaryParams,
    ToolResult,
    match_state,
    shot_summary,
)
from second_screen_pundit.agents.signals import SignalTracker
from second_screen_pundit.agents.tools.runtime import ToolResultCapture
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents import speech
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event


def _settings(mode: str) -> AgentSettings:
    return AgentSettings(
        mode=mode,  # type: ignore[arg-type]
        endpoint=None,
        model_fast=None,
        model_deep=None,
        speech_endpoint=None,
        speech_region=None,
        live_timeout_seconds=15,
        explainer_timeout_seconds=20,
        recap_timeout_seconds=45,
        importance_threshold=0.45,
        cooldown_match_minutes=4,
    )


def _score_at_two_behind(events: list[Event]) -> tuple[Event, int]:
    away_goals = [
        event
        for event in events
        if event.type == "goal" and event.team == "away"
    ]
    return away_goals[1], away_goals[1].seq


def test_two_ahead_is_grounded_by_cited_zero_two_score() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    goal, as_of = _score_at_two_behind(events)
    results = _cached_tool_results(
        events,
        as_of=as_of,
        fact_ids=["match_state:score"],
    )
    explanation = Explanation(
        answer="Port Calder are two ahead.",
        claims=[
            Claim(
                text="Port Calder are two ahead.",
                fact_ids=["match_state:score"],
                event_ids=[goal.event_id],
            )
        ],
        fact_ids=["match_state:score"],
        event_ids=[goal.event_id],
    )

    report = check_evidence(
        explanation,
        events=events,
        as_of=as_of,
        tool_results=results,
        info=info,
    )

    assert report.passed, report.failures


def test_three_ahead_is_rejected_when_cited_score_is_zero_two() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    goal, as_of = _score_at_two_behind(events)
    results = _cached_tool_results(
        events,
        as_of=as_of,
        fact_ids=["match_state:score"],
    )
    explanation = Explanation(
        answer="Port Calder are three ahead.",
        claims=[
            Claim(
                text="Port Calder are three ahead.",
                fact_ids=["match_state:score"],
                event_ids=[goal.event_id],
            )
        ],
        fact_ids=["match_state:score"],
        event_ids=[goal.event_id],
    )

    report = check_evidence(
        explanation,
        events=events,
        as_of=as_of,
        tool_results=results,
        info=info,
    )

    assert not report.passed
    assert any("ungrounded number 'three'" in failure for failure in report.failures)


def test_team_statistics_are_not_grounded_by_a_score_fact() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    goal = next(event for event in events if event.type == "goal")
    score = Fact(
        fact_id="match_state:review-case:score",
        label="Match-level score through 90'",
        value="2-1",
        event_ids=[goal.event_id],
    )
    line = CommentaryLine(
        line_id=f"m01-l{goal.seq:03d}",
        text="Ashbridge Rovers have had 2 shots and 1 on target.",
        insight_id="m01-i0001",
        fact_ids=[score.fact_id],
        event_ids=[goal.event_id],
        tone="measured",
    )

    report = check_evidence(
        line,
        events=events,
        as_of=goal.seq,
        tool_results=[ToolResult(tool="match_state", facts=[score])],
        info=info,
    )

    assert not report.passed
    assert sum("ungrounded number" in failure for failure in report.failures) == 2


def test_team_statistics_pass_when_their_facts_and_team_are_cited() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    as_of = events[-1].seq
    stats = shot_summary(
        events,
        ShotSummaryParams(team="home", start=0, end=91),
        as_of=as_of,
    )
    shots, on_target = stats.facts[:2]
    line = CommentaryLine(
        line_id=f"m01-l{as_of:03d}",
        text=(
            f"{info.home.name} have had {shots.value} shots and "
            f"{on_target.value} on target."
        ),
        insight_id="m01-i0001",
        fact_ids=[shots.fact_id, on_target.fact_id],
        event_ids=list(dict.fromkeys([*shots.event_ids, *on_target.event_ids])),
        tone="measured",
    )

    report = check_evidence(
        line,
        events=events,
        as_of=as_of,
        tool_results=[stats],
        info=info,
    )

    assert report.passed, report.failures


def test_fact_ids_are_unique_across_calls_and_duplicates_are_rejected() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    as_of = events[-1].seq
    params = ShotSummaryParams(team="home", start=0, end=45)
    first = shot_summary(events, params, as_of=as_of)
    second = shot_summary(events, params, as_of=as_of)
    first_ids = {fact.fact_id for fact in first.facts}
    second_ids = {fact.fact_id for fact in second.facts}
    assert first_ids.isdisjoint(second_ids)

    shots, on_target = first.facts[:2]
    line = CommentaryLine(
        line_id=f"m01-l{as_of:03d}",
        text=(
            f"{info.home.name} have had {shots.value} shots and "
            f"{on_target.value} on target."
        ),
        insight_id="m01-i0001",
        fact_ids=[shots.fact_id, on_target.fact_id],
        event_ids=list(dict.fromkeys([*shots.event_ids, *on_target.event_ids])),
        tone="measured",
    )
    report = check_evidence(
        line,
        events=events,
        as_of=as_of,
        tool_results=[first, second],
        info=info,
    )
    assert report.passed, report.failures

    duplicated = second.model_copy(
        update={
            "facts": [
                fact.model_copy(update={"fact_id": first.facts[index].fact_id})
                for index, fact in enumerate(second.facts)
            ]
        }
    )
    duplicate_report = check_evidence(
        line,
        events=events,
        as_of=as_of,
        tool_results=[first, duplicated],
        info=info,
    )
    assert not duplicate_report.passed
    assert any("duplicate fact_id within run" in failure for failure in duplicate_report.failures)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("question", RuntimeError("HTTP 429 throttled")),
        ("question", MiddlewareFailure("invalid minute=93 argument")),
        ("recap", RuntimeError("HTTP 429 throttled")),
        ("recap", MiddlewareFailure("invalid minute=93 argument")),
    ],
)
def test_question_and_recap_fall_back_after_agent_or_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error: Exception,
) -> None:
    class FailingAgent:
        async def run(self, _: object) -> object:
            raise error

    class PassthroughWorkflow:
        def __init__(self, *, start_executor: object) -> None:
            self.agent = start_executor

        def build(self) -> PassthroughWorkflow:
            return self

        async def run(self, message: object) -> object:
            return await self.agent.run(message.contents)  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "second_screen_pundit.agents.pipeline.AgentExecutor",
        lambda agent: agent,
    )
    monkeypatch.setattr(
        "second_screen_pundit.agents.pipeline.WorkflowBuilder",
        PassthroughWorkflow,
    )
    events = generate_match("m01")
    info = get_match_info("m01")
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]
    monkeypatch.setattr(pipeline, "_agent", lambda *_args, **_kwargs: FailingAgent())
    as_of = events[-1].seq

    if operation == "question":
        result = asyncio.run(
            pipeline.answer_question(info, events, "Why did the shape change?", as_of=as_of)
        )
        notice = result.explanation.notice
    else:
        result = asyncio.run(pipeline.produce_recap(info, events, as_of=as_of))
        notice = result.recap.notice

    assert result.attempts == 2
    assert result.evidence.passed
    assert notice is not None and type(error).__name__ in notice


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Port Calder lead 1-0.", True),
        ("Port Calder lead 0-1.", False),
    ],
)
def test_away_lead_score_uses_leading_team_perspective(
    text: str,
    expected: bool,
) -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    goal = next(event for event in events if event.type == "goal" and event.minute == 23)
    results = _cached_tool_results(
        events,
        as_of=goal.seq,
        fact_ids=["match_state:score"],
    )
    line = CommentaryLine(
        line_id=f"m01-l{goal.seq:03d}",
        text=text,
        insight_id="m01-i0395",
        fact_ids=["match_state:score"],
        event_ids=[goal.event_id],
        tone="measured",
    )

    report = check_evidence(
        line,
        events=events,
        as_of=goal.seq,
        tool_results=results,
        info=info,
    )

    assert report.passed is expected
    if not expected:
        assert any("away-leading commentary" in failure for failure in report.failures)


def test_future_event_is_rejected() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    future_goal = next(event for event in events if event.type == "goal")
    explanation = Explanation(
        answer="A goal has been scored.",
        claims=[
            Claim(
                text="A goal has been scored.",
                event_ids=[future_goal.event_id],
            )
        ],
        event_ids=[future_goal.event_id],
    )

    report = check_evidence(
        explanation,
        events=events,
        as_of=future_goal.seq - 1,
        tool_results=[],
        info=info,
    )

    assert not report.passed
    assert any("future event_id" in failure for failure in report.failures)


def test_tool_middleware_maps_wrapper_name_to_analytics_tool() -> None:
    events = generate_match("m01")
    result = match_state(events, MatchStateParams(minute=23), as_of=events[-1].seq)
    context = SimpleNamespace(
        function=SimpleNamespace(name="match_state_tool"),
        result=[SimpleNamespace(text=result.model_dump_json())],
    )
    capture = ToolResultCapture()

    async def call_next() -> None:
        return None

    asyncio.run(capture.process(context, call_next))

    assert capture.results == [result]


def test_workflow_model_extraction_accepts_executor_and_direct_responses() -> None:
    insight = Insight(
        insight_id="m01-i0001",
        minute=1,
        headline="An early moment",
        importance=0.5,
        event_ids=["m01-e0001"],
    )
    direct_response = SimpleNamespace(value=insight)
    wrapped_response = SimpleNamespace(
        agent_response=SimpleNamespace(value=insight)
    )
    run_result = SimpleNamespace(
        get_intermediate_outputs=lambda: [wrapped_response],
        get_outputs=lambda: [direct_response],
    )

    assert _workflow_models(run_result) == [insight, insight]


def test_must_say_window_uses_deterministic_insight_and_one_commentator_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    goal = next(event for event in events if event.type == "goal" and event.minute == 23)
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]
    roles: list[str] = []
    supplied: list[dict[str, object]] = []

    class StubAgent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            supplied.append(payload)
            insight = payload["insight"]
            return SimpleNamespace(
                value=CommentaryLine(
                    line_id=f"m01-l{goal.seq:03d}",
                    text=f"{payload['score_phrase']}.",
                    insight_id=insight["insight_id"],
                    fact_ids=insight["fact_ids"],
                    event_ids=insight["event_ids"],
                    tone="measured",
                )
            )

    def fake_agent(role: str, *_: object, **__: object) -> StubAgent:
        roles.append(role)
        return StubAgent()

    monkeypatch.setattr(pipeline, "_agent", fake_agent)
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq)
    )

    assert result.status == "commentary"
    assert roles == ["commentator"]
    assert supplied[0]["score_phrase"] == "Port Calder lead 1-0"
    assert [timing.role for timing in result.timings] == ["commentator"]
    assert result.evidence is not None and result.evidence.passed


def test_goal_fast_path_cites_linked_scoring_shot_and_goal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    goal = next(event for event in events if event.type == "goal" and event.minute == 23)
    shot_id = next(iter(goal.related_event_ids))
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]

    class StubAgent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            insight = payload["insight"]
            return SimpleNamespace(
                value=CommentaryLine(
                    line_id=f"m01-l{goal.seq:03d}",
                    text=f"{payload['score_phrase']}.",
                    insight_id=insight["insight_id"],
                    fact_ids=insight["fact_ids"],
                    event_ids=insight["event_ids"],
                    tone="measured",
                )
            )

    roles: list[str] = []

    def fake_agent(role: str, *_: object, **__: object) -> StubAgent:
        roles.append(role)
        return StubAgent()

    monkeypatch.setattr(pipeline, "_agent", fake_agent)
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq)
    )

    assert result.status == "commentary"
    assert roles == ["commentator"]
    assert shot_id in result.line.event_ids  # type: ignore[union-attr]
    assert goal.event_id in result.line.event_ids  # type: ignore[union-attr]
    shot_fact = next(
        fact
        for item in result.tool_results
        for fact in item.facts
        if "scoring shot xG" in fact.label
    )
    assert shot_fact.event_ids == [shot_id]


def test_stale_tactical_window_drops_before_commentator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    event = next(event for event in events if event.type == "pass")
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]
    roles: list[str] = []
    stale_checks = 0

    class StubAgent:
        def __init__(self, role: str) -> None:
            self.role = role

        async def run(self, _: str) -> SimpleNamespace:
            if self.role == "director":
                value = DirectorDecision(
                    should_comment=True,
                    importance=0.9,
                    reason="A tactical window merits commentary.",
                    event_ids=[event.event_id],
                )
            else:
                value = Insight(
                    insight_id="m01-i0001",
                    minute=event.minute,
                    headline="A tactical change",
                    importance=0.9,
                    event_ids=[event.event_id],
                )
            return SimpleNamespace(value=value)

    def fake_agent(role: str, *_: object, **__: object) -> StubAgent:
        roles.append(role)
        return StubAgent(role)

    def is_stale() -> bool:
        nonlocal stale_checks
        stale_checks += 1
        return stale_checks == 2

    monkeypatch.setattr(pipeline, "_agent", fake_agent)
    result = asyncio.run(
        pipeline.comment_on_window(
            info,
            events,
            [event],
            as_of=event.seq,
            is_stale=is_stale,
        )
    )

    assert result.status == "skipped"
    assert "became stale" in (result.notice or "")
    assert roles == ["director", "analyst"]
    assert [timing.role for timing in result.timings] == ["director", "analyst"]


def _tactical_stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    commentator_failures: int,
) -> tuple[PunditPipeline, list[str], list[Event], list[Event], int]:
    info_events = generate_match("m01")
    window_event = next(
        event for event in info_events if event.minute == 30 and event.type == "pass"
    )
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]
    roles: list[str] = []
    commentator_calls = 0
    truncated = None
    try:
        CommentaryLine.model_validate_json('{"fact_ids":["match_state:score"')
    except Exception as exc:  # noqa: BLE001 - captures the ValidationError a truncated reply raises
        truncated = exc

    class TruncatedResponse:
        @property
        def value(self) -> object:
            raise truncated  # type: ignore[misc]

    class StubAgent:
        def __init__(self, role: str) -> None:
            self.role = role

        async def run(self, prompt: str) -> object:
            nonlocal commentator_calls
            payload = json.loads(prompt)
            if self.role == "director":
                return SimpleNamespace(
                    value=DirectorDecision(
                        should_comment=True,
                        importance=0.9,
                        reason="A meaningful shift.",
                        event_ids=[window_event.event_id],
                    )
                )
            if self.role == "analyst":
                return SimpleNamespace(
                    value=Insight(
                        insight_id="m01-i0001",
                        minute=30,
                        headline="Port Calder remain ahead",
                        importance=0.7,
                    )
                )
            commentator_calls += 1
            if commentator_calls <= commentator_failures:
                return TruncatedResponse()
            insight = payload["insight"]
            return SimpleNamespace(
                value=CommentaryLine(
                    line_id=f"m01-l{window_event.seq:03d}",
                    text=f"{payload['score_phrase']}.",
                    insight_id=insight["insight_id"],
                    fact_ids=insight["fact_ids"],
                    event_ids=insight["event_ids"],
                    tone="measured",
                )
            )

    def fake_agent(role: str, *_: object, **__: object) -> StubAgent:
        roles.append(role)
        return StubAgent(role)

    monkeypatch.setattr(pipeline, "_agent", fake_agent)
    return pipeline, roles, info_events, [window_event], window_event.seq


def test_truncated_commentator_reply_retries_only_the_commentator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, roles, events, window, as_of = _tactical_stub_pipeline(monkeypatch, 1)

    result = asyncio.run(
        pipeline.comment_on_window(get_match_info("m01"), events, window, as_of=as_of)
    )

    assert result.status == "commentary", (result.notice, result.evidence)
    assert roles == ["director", "analyst", "commentator", "commentator"]
    assert [t.attempt for t in result.timings if t.role == "commentator"] == [1, 2]
    assert [t.role for t in result.timings].count("director") == 1


def test_two_truncated_replies_are_reported_as_invalid_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, roles, events, window, as_of = _tactical_stub_pipeline(monkeypatch, 2)

    result = asyncio.run(
        pipeline.comment_on_window(get_match_info("m01"), events, window, as_of=as_of)
    )

    assert result.status == "rejected"
    assert result.notice == "Structured output was invalid on both attempts."
    assert roles.count("commentator") == 2 and roles.count("analyst") == 1


def test_cached_outputs_pass_evidence_checks_for_all_storylines() -> None:
    pipeline = PunditPipeline(_settings("cached"), None)
    for match_id in ("m01", "m02", "m03"):
        info = get_match_info(match_id)
        events = generate_match(match_id)
        event_by_id = {event.event_id: event for event in events}
        cached = load_cached_match(match_id)

        for line in cached.commentary:
            as_of = max(event_by_id[event_id].seq for event_id in line.event_ids)
            result = asyncio.run(
                pipeline.comment_on_window(
                    info,
                    events,
                    [event_by_id[line.event_ids[-1]]],
                    as_of=as_of,
                )
            )
            assert result.evidence is not None, (match_id, line.line_id)
            assert result.evidence.passed, (match_id, line.line_id, result.evidence.failures)

        final_as_of = events[-1].seq
        for question in cached.questions:
            result = asyncio.run(
                pipeline.answer_question(
                    info,
                    events,
                    question.question,
                    as_of=final_as_of,
                )
            )
            assert result.evidence.passed, (match_id, question.question, result.evidence.failures)

        recap = asyncio.run(pipeline.produce_recap(info, events, as_of=final_as_of))
        assert recap.evidence.passed, (match_id, recap.evidence.failures)


def test_shared_factory_sets_required_options_for_each_role(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

    monkeypatch.setattr("second_screen_pundit.agents.clients.Agent", FakeAgent)
    monkeypatch.setattr("second_screen_pundit.agents.clients.FoundryChatClient", FakeClient)
    for role in ("director", "analyst", "commentator", "explainer", "recap"):
        create_foundry_agent(
            role,  # type: ignore[arg-type]
            endpoint="https://example.invalid/project",
            model_fast="fast",
            model_deep="deep",
            credential=object(),  # type: ignore[arg-type]
            instructions="instructions",
        )
        options = captured["default_options"]
        assert options["store"] is False
        assert options["include"] == ["reasoning.encrypted_content"]
        if role in {"director", "commentator"}:
            assert options["reasoning"] == {"effort": "low"}
        else:
            assert "reasoning" not in options


def test_factory_honours_reasoning_override_and_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    def fail_client(**_: object) -> None:
        raise AssertionError("a supplied client must be reused, not rebuilt")

    shared = object()
    monkeypatch.setattr("second_screen_pundit.agents.clients.Agent", FakeAgent)
    monkeypatch.setattr("second_screen_pundit.agents.clients.FoundryChatClient", fail_client)
    create_foundry_agent(
        "commentator",
        endpoint="https://example.invalid/project",
        model_fast="fast",
        model_deep="deep",
        credential=object(),  # type: ignore[arg-type]
        instructions="instructions",
        reasoning_effort="none",
        client=shared,  # type: ignore[arg-type]
    )

    assert captured["client"] is shared
    assert captured["default_options"]["reasoning"] == {"effort": "none"}  # type: ignore[index]


def test_mandatory_signal_skips_director_and_cites_signal_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events = generate_match("m01")
    change = next(
        event
        for event in events
        if event.type == "formation_change" and event.detail.get("formation") == "3-4-3"
    )
    signal = next(
        signal
        for signal in SignalTracker(info).evaluate(events, as_of=change.seq)
        if signal.kind == "formation_change" and signal.facts[0].value == "3-4-3"
    )
    window = [next(event for event in reversed(events) if event.seq < change.seq and event.type == "pass")]
    pipeline = PunditPipeline(_settings("live"), object())  # type: ignore[arg-type]
    roles: list[str] = []
    prompts: dict[str, dict[str, object]] = {}

    class StubAgent:
        def __init__(self, role: str) -> None:
            self.role = role

        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            prompts[self.role] = payload
            if self.role == "analyst":
                value: object = Insight(
                    insight_id=f"m01-i{change.seq:04d}",
                    minute=72,
                    headline="A shape change",
                    importance=0.8,
                    fact_ids=[fact.fact_id for fact in signal.facts],
                    event_ids=signal.event_ids,
                )
            else:
                insight = payload["insight"]
                value = CommentaryLine(
                    line_id=f"m01-l{change.seq:03d}",
                    text=f"{payload['score_phrase']}.",
                    insight_id=insight["insight_id"],
                    fact_ids=insight["fact_ids"],
                    event_ids=insight["event_ids"],
                    tone="analytical",
                )
            return SimpleNamespace(value=value)

    def fake_agent(role: str, *_: object, **__: object) -> StubAgent:
        roles.append(role)
        return StubAgent(role)

    monkeypatch.setattr(pipeline, "_agent", fake_agent)
    result = asyncio.run(
        pipeline.comment_on_window(
            info, events, window, as_of=change.seq, signals=[signal]
        )
    )

    assert result.status == "commentary", result.evidence
    assert roles == ["analyst", "commentator"]
    assert "events" in prompts["commentator"] and prompts["commentator"]["events"] == []
    assert {fact["fact_id"] for fact in prompts["commentator"]["facts"]} >= {  # type: ignore[union-attr]
        fact.fact_id for fact in signal.facts
    }
    assert prompts["analyst"]["signals"][0]["kind"] == "formation_change"  # type: ignore[index]


def test_speech_synthesizer_uses_entra_and_emits_mp3(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    class FakeSpeechConfig:
        def __init__(self, **kwargs: object) -> None:
            observed["config"] = kwargs

        def set_speech_synthesis_output_format(self, output_format: object) -> None:
            observed["format"] = output_format

    class FakeSpeechSynthesizer:
        def __init__(self, **kwargs: object) -> None:
            observed["synthesizer"] = kwargs

        def speak_text_async(self, text: str) -> SimpleNamespace:
            observed["text"] = text
            return SimpleNamespace(
                get=lambda: SimpleNamespace(
                    reason="completed",
                    audio_duration=timedelta(seconds=121),
                    audio_data=b"fake-mp3",
                )
            )

    monkeypatch.setattr(
        speech,
        "speechsdk",
        SimpleNamespace(
            SpeechConfig=FakeSpeechConfig,
            SpeechSynthesisOutputFormat=SimpleNamespace(
                Audio24Khz160KBitRateMonoMp3="mp3"
            ),
            SpeechSynthesizer=FakeSpeechSynthesizer,
            ResultReason=SimpleNamespace(SynthesizingAudioCompleted="completed"),
            SpeechSynthesisCancellationDetails=SimpleNamespace(),
        ),
    )
    artifact = speech.SpeechSynthesizer(
        endpoint="https://speech.example.invalid/",
        region="australiaeast",
        credential=object(),  # type: ignore[arg-type]
    ).synthesize_mp3("A synthetic commentary line.", tmp_path / "commentary.mp3")

    assert observed["config"]["token_credential"] is not None  # type: ignore[index]
    assert observed["config"]["endpoint"] == "https://speech.example.invalid/"  # type: ignore[index]
    assert "region" not in observed["config"]  # type: ignore[operator]
    assert observed["synthesizer"]["audio_config"] is None  # type: ignore[index]
    assert observed["format"] == "mp3"
    assert observed["text"] == "A synthetic commentary line."
    assert artifact.duration_seconds == 121
    assert artifact.path.read_bytes() == b"fake-mp3"
    assert not (tmp_path / "commentary.partial.mp3").exists()
