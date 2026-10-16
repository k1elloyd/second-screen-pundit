from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents.cache import load_cached_match
from second_screen_pundit.agents.clients import create_foundry_agent
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.models import (
    Claim,
    CommentaryLine,
    Explanation,
    Insight,
)
from second_screen_pundit.agents.pipeline import (
    PunditPipeline,
    _cached_tool_results,
    _workflow_models,
)
from second_screen_pundit.agents.tools.analytics import MatchStateParams, match_state
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
    assert observed["format"] == "mp3"
    assert observed["text"] == "A synthetic commentary line."
    assert artifact.duration_seconds == 121
    assert artifact.path.read_bytes() == b"fake-mp3"
    assert not (tmp_path / "commentary.partial.mp3").exists()
