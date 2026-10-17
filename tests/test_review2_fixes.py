"""Review 2 fixes: viewer Q&A, recap, formation after the second-half kick-off."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from second_screen_pundit.agents.clients import _MAX_TOKENS
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.models import ExplainerReply, RecapMoment, RecapReply
from second_screen_pundit.agents.pipeline import PunditPipeline, full_time_score_phrase
from second_screen_pundit.agents.tools.analytics import formation_at, match_snapshot, match_state
from second_screen_pundit.agents.tools.parameters import FormationAtParams, MatchStateParams
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event


def _settings() -> AgentSettings:
    return AgentSettings(
        mode="live",
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


def _as_of_at(events: list[Event], minute: int) -> int:
    return max(event.seq for event in events if event.minute + event.added_minute <= minute)


class _ScriptedAgent:
    """Stands in for a Foundry agent: each run returns the next scripted reply or raises it."""

    def __init__(self, replies: list, prompts: list[dict], tools: list) -> None:
        self.replies = replies
        self.prompts = prompts
        self.tools = tools

    async def run(self, prompt: str) -> object:
        self.prompts.append(json.loads(prompt))
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(self.prompts[-1])
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(value=reply, finish_reason="stop", usage_details={})


class _OneAgentWorkflow:
    def __init__(self, *, start_executor: _ScriptedAgent) -> None:
        self.agent = start_executor

    def build(self) -> _OneAgentWorkflow:
        return self

    async def run(self, message: object) -> object:
        response = await self.agent.run(message.text)  # type: ignore[attr-defined]
        return SimpleNamespace(get_outputs=lambda: [SimpleNamespace(agent_response=response)])


def _scripted_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    replies: list,
) -> tuple[PunditPipeline, list[dict], list[list[str]]]:
    monkeypatch.setattr("second_screen_pundit.agents.pipeline.AgentExecutor", lambda agent: agent)
    monkeypatch.setattr("second_screen_pundit.agents.pipeline.WorkflowBuilder", _OneAgentWorkflow)
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    prompts: list[dict] = []
    tool_names: list[list[str]] = []

    def agent(_role, _instructions, *, tools=(), middleware=(), time_budget):
        del middleware, time_budget
        tool_names.append([tool.__name__ for tool in tools])
        return _ScriptedAgent(replies, prompts, list(tools))

    monkeypatch.setattr(pipeline, "_agent", agent)
    return pipeline, prompts, tool_names


def _alias(prompt: dict, label_start: str) -> dict:
    return next(
        item
        for item in [*prompt["facts"], *prompt["key_moments"]]
        if str(item["label"]).startswith(label_start)
    )


def _truncated_reply_error() -> Exception:
    try:
        ExplainerReply.model_validate_json('{"answer": "Port Calder have', strict=True)
    except ValidationError as exc:
        error = RuntimeError("structured output failed")
        error.__cause__ = exc
        return error
    raise AssertionError("the truncated reply parsed")


# Formation reset (finding 3)


def test_second_half_kickoff_keeps_m02_home_formation_after_the_red_card() -> None:
    events = generate_match("m02")
    second_half_kickoff = next(
        event for event in events if event.type == "kickoff" and event.period == 2
    )
    as_of = _as_of_at(events, 60)

    formation = formation_at(events, FormationAtParams(team="home", minute=60), as_of=as_of)
    state = match_state(events, MatchStateParams(minute=60), as_of=as_of)
    home_state = next(fact for fact in state.facts if fact.label == "Port Calder FC formation through 60'")

    assert "formation" not in second_half_kickoff.detail
    assert formation.facts[0].value == "4-4-1"
    assert home_state.value == "4-4-1"


def test_formation_ignores_a_formation_on_a_restart_kickoff() -> None:
    """Event logs generated before the fix still carry a formation on the 46' kick-off."""
    events = generate_match("m02")
    stale = [
        event.model_copy(update={"detail": {"formation": "4-2-3-1"}})
        if event.type == "kickoff" and event.period == 2
        else event
        for event in events
    ]

    result = formation_at(stale, FormationAtParams(team="home", minute=60), as_of=_as_of_at(stale, 60))

    assert result.facts[0].value == "4-4-1"


# Viewer Q&A (findings 1 and 4)


def test_explainer_caps_leave_room_for_low_effort_reasoning() -> None:
    assert _MAX_TOKENS["explainer"] >= 1500
    assert _MAX_TOKENS["recap"] >= 1500


def test_snapshot_reflects_the_viewer_position_only() -> None:
    events = generate_match("m02")
    as_of = _as_of_at(events, 30)
    results = match_snapshot(events, as_of=as_of)
    labels = [fact.label for result in results for fact in result.facts]

    assert "Match-level clock at 30'" in labels
    assert "Port Calder FC formation through 30'" in labels
    assert "Port Calder FC goal at 27'" in labels
    assert not any("red card" in label for label in labels)  # 38', not yet released
    assert all(
        event_id <= f"m02-e{as_of:04d}"
        for result in results
        for fact in result.facts
        for event_id in fact.event_ids
    )


def test_question_is_answered_from_the_snapshot_in_one_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")
    as_of = _as_of_at(events, 84)

    def answer(prompt: dict) -> ExplainerReply:
        formation = _alias(prompt, "Port Calder FC formation through")
        red_card = _alias(prompt, "Port Calder FC red card at")
        return ExplainerReply(
            answer=(
                f"Port Calder FC are playing a {formation['value']}, the shape they moved to "
                f"after the red card in the {red_card['minute']}th minute."
            ),
            fact_ids=[formation["id"], red_card["id"]],
        )

    pipeline, prompts, tool_names = _scripted_pipeline(monkeypatch, [answer])
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "What formation are Port Calder playing now?", as_of=as_of
        )
    )

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 1 and len(prompts) == 1
    assert prompts[0]["viewer_clock"] == "84'"
    assert prompts[0]["score_phrase"] == "Merehaven Athletic lead 2-1"
    assert "xg_timeline_tool" not in tool_names[0]
    explanation = result.explanation
    assert "4-4-1" in explanation.answer
    assert all(not fact_id.startswith("F") for fact_id in explanation.fact_ids)
    red_card = next(event for event in events if event.type == "card" and event.minute == 38)
    assert red_card.event_id in explanation.event_ids
    assert [claim.text for claim in explanation.claims][0] == "Port Calder FC formation: 4-4-1."
    assert [timing.finish_reason for timing in result.timings] == ["stop"]


def test_snapshot_headline_facts_ground_the_answer_without_being_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 50' failure: score and previous formation stated, neither fact listed."""
    events = generate_match("m02")
    info = get_match_info("m02")

    def answer(prompt: dict) -> ExplainerReply:
        players = _alias(prompt, "Port Calder FC players on the pitch")
        return ExplainerReply(
            answer=(
                f"{prompt['score_phrase']}, but Port Calder FC went from 4-2-3-1 to 4-4-1 after "
                f"the red card in the 38th minute and have {players['value']} players."
            ),
            fact_ids=[players["id"]],
        )

    pipeline, _, _ = _scripted_pipeline(monkeypatch, [answer])
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "What changed after the red card?", as_of=_as_of_at(events, 50)
        )
    )

    assert result.source == "agent", result.evidence.failures
    # Session B: the claims are the facts the answer used, the model's own citation first,
    # not only the ones it listed.
    claims = [claim.text for claim in result.explanation.claims]
    assert claims[0] == "Port Calder FC players on the pitch: 10 players."
    assert sorted(claims[1:]) == sorted(
        [
            "Match-level score: 1-0.",
            "Port Calder FC formation: 4-4-1.",
            "Port Calder FC formation before the change: 4-2-3-1.",
            "Port Calder FC red card: red card.",
        ]
    )


def test_snapshot_window_is_a_minute_range_not_a_score_and_stats_need_no_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 74' failure: "59-75'" was read as a score, and snapshot stats went unlisted."""
    events = generate_match("m02")
    info = get_match_info("m02")

    def answer(prompt: dict) -> ExplainerReply:
        recent = _alias(prompt, "Merehaven Athletic possession, ")
        window = recent["label"].split(", ")[1]
        shots = _alias(prompt, "Merehaven Athletic shots through")
        return ExplainerReply(
            answer=(
                f"Merehaven Athletic have {recent['value']}% of the possession in {window} and "
                f"{shots['value']} shots, so they are camped in the final third."
            ),
            fact_ids=[],
        )

    pipeline, _, _ = _scripted_pipeline(monkeypatch, [answer])
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "Why are Merehaven camped in the final third?", as_of=_as_of_at(events, 74)
        )
    )

    assert result.source == "agent", result.evidence.failures
    # Session B: the two stats the answer used are cited, though the model listed none.
    assert [claim.text for claim in result.explanation.claims] == [
        "Merehaven Athletic possession: 55.8 %.",
        "Merehaven Athletic shots: 27 shots.",
    ]


def test_question_retries_after_an_exception_with_the_parse_error_as_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def answer(prompt: dict) -> ExplainerReply:
        xg = _alias(prompt, "Merehaven Athletic xG through")
        return ExplainerReply(
            answer=f"Merehaven Athletic have had the better chances, with {xg['value']} xG.",
            fact_ids=[xg["id"]],
        )

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [_truncated_reply_error(), answer])
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "Who has had the better chances?", as_of=_as_of_at(events, 74)
        )
    )

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 2
    assert prompts[0]["retry_feedback"] == ""
    assert "cut off" in prompts[1]["retry_feedback"]
    assert [timing.finish_reason for timing in result.timings] == ["length", "stop"]


def test_unknown_fact_alias_gets_feedback_and_never_reaches_the_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")
    bad = ExplainerReply(answer="Merehaven have 99 shots.", fact_ids=["F999"])

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [bad, bad])
    result = asyncio.run(
        pipeline.answer_question(info, events, "How many shots?", as_of=_as_of_at(events, 50))
    )

    assert result.source == "fallback"
    assert "F999" in prompts[1]["retry_feedback"]
    assert "99" not in result.explanation.answer
    assert result.explanation.notice is not None
    assert "Exception" not in result.explanation.notice


# Recap (finding 6)


def test_full_time_score_phrase_puts_the_winner_first() -> None:
    info = get_match_info("m02")

    assert full_time_score_phrase(info, "1-2") == "Merehaven Athletic win 2-1"
    assert full_time_score_phrase(info, "2-0") == "Port Calder win 2-0"
    assert full_time_score_phrase(info, "1-1") == "Port Calder and Merehaven Athletic draw 1-1"


def _ordinal(minute: int) -> str:
    suffix = "th" if 10 <= minute % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(minute % 10, "th")
    return f"{minute}{suffix}"


def _recap_following_the_prompt(prompt: dict) -> RecapReply:
    """A reply that does what the instructions ask, citing one event ID in the wrong list."""
    phrase = prompt["score_phrase"]
    moments = prompt["key_moments"]
    chosen = [moments[0], moments[len(moments) // 2], moments[-1]]
    return RecapReply(
        headline=f"{phrase} at {prompt['match']['stadium']}",
        summary=(
            f"{phrase}. {chosen[0]['description'].capitalize()} in the "
            f"{_ordinal(chosen[0]['minute'])} minute, and the decisive moment came in the "
            f"{_ordinal(chosen[-1]['minute'])} minute."
        ),
        key_moments=[
            RecapMoment(
                minute=moment["minute"],
                text=f"{moment['description']}.",
                event_ids=moment["event_ids"],
            )
            for moment in chosen
        ],
        audio_script=f"Full time: {phrase}.",
        fact_ids=[chosen[0]["id"], chosen[-1]["id"], chosen[1]["event_ids"][0]],
        event_ids=[],
    )


def test_m01_recap_is_agent_written(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m01")
    info = get_match_info("m01")

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [_recap_following_the_prompt])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 1
    assert result.recap.notice is None
    assert prompts[0]["score_phrase"] == "Ashbridge Rovers win 3-2"
    assert {"Match-level clock at 90'", "Match-level minutes remaining at 90'"} <= {
        fact["label"] for fact in prompts[0]["facts"]
    }
    assert all(moment["event_ids"] for moment in prompts[0]["key_moments"])
    assert result.recap.headline.startswith("Ashbridge Rovers win 3-2")
    # The event ID cited among fact_ids was moved to event_ids, not rejected.
    moved = prompts[0]["key_moments"][len(prompts[0]["key_moments"]) // 2]["event_ids"][0]
    assert moved in result.recap.event_ids
    assert moved not in result.recap.fact_ids


def test_recap_retries_with_feedback_naming_an_unknown_id(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def unknown_id(prompt: dict) -> RecapReply:
        return _recap_following_the_prompt(prompt).model_copy(update={"fact_ids": ["m02-x9999"]})

    pipeline, prompts, _ = _scripted_pipeline(
        monkeypatch, [unknown_id, _recap_following_the_prompt]
    )
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 2
    assert "m02-x9999" in prompts[1]["retry_feedback"]
    assert prompts[1]["score_phrase"] == "Merehaven Athletic win 2-1"


@pytest.mark.live
def test_live_m01_recap_is_agent_written() -> None:
    from azure.identity import DefaultAzureCredential
    from dotenv import load_dotenv

    load_dotenv()
    events = generate_match("m01")
    info = get_match_info("m01")
    pipeline = PunditPipeline(AgentSettings.from_environment(), DefaultAzureCredential())

    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    print(result.recap.model_dump_json(indent=2))
    print([(timing.attempt, timing.duration_ms, timing.finish_reason) for timing in result.timings])
    assert result.source == "agent", result.evidence.failures
