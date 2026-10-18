"""Phase 3 Session B, group 2: citations, recap wording and timing."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.models import Explanation, ExplainerReply, RecapReply
from second_screen_pundit.agents.pipeline import (
    PunditPipeline,
    _cite_used_facts,
    full_time_score_phrase,
)
from second_screen_pundit.agents.tools.analytics import Fact, ToolResult, match_snapshot
from second_screen_pundit.api import ReplaySession, create_app
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.replay import Replay
from second_screen_pundit.engine.schema import Event
from tests.test_api import _settings as _api_settings
from tests.test_line_audio import _run as _run_line_audio
from tests.test_line_audio import _Synth
from tests.test_review2_fixes import (
    _alias,
    _as_of_at,
    _recap_following_the_prompt,
    _scripted_pipeline,
    _settings,
)


# Q&A citations


@pytest.mark.parametrize("model_lists", ["the stat", "nothing", "the stat and two unused facts"])
def test_an_answer_that_uses_one_stat_cites_exactly_that_fact(
    monkeypatch: pytest.MonkeyPatch, model_lists: str
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def answer(prompt: dict) -> ExplainerReply:
        shots = _alias(prompt, "Merehaven Athletic shots through")
        unused = [
            _alias(prompt, "Port Calder FC formation through")["id"],
            _alias(prompt, "Merehaven Athletic xG through")["id"],
        ]
        listed = {
            "the stat": [shots["id"]],
            "nothing": [],
            "the stat and two unused facts": [shots["id"], *unused],
        }[model_lists]
        return ExplainerReply(
            answer=f"Merehaven Athletic have had {shots['value']} shots, so they keep coming.",
            fact_ids=listed,
        )

    pipeline, _, _ = _scripted_pipeline(monkeypatch, [answer])
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "Are Merehaven pushing?", as_of=_as_of_at(events, 74)
        )
    )

    assert result.source == "agent", result.evidence.failures
    explanation = result.explanation
    assert len(explanation.fact_ids) == 1
    assert "merehaven_athletic_shots" in explanation.fact_ids[0]
    assert [claim.fact_ids for claim in explanation.claims] == [explanation.fact_ids]
    assert result.evidence.passed


def test_an_answer_with_no_numbers_cites_no_snapshot_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")
    reply = ExplainerReply(answer="Merehaven Athletic have had more of the ball lately.", fact_ids=[])
    pipeline, _, _ = _scripted_pipeline(monkeypatch, [reply])
    result = asyncio.run(
        pipeline.answer_question(info, events, "Who is on top?", as_of=_as_of_at(events, 74))
    )
    assert result.source == "agent", result.evidence.failures
    assert result.explanation.fact_ids == []
    assert result.explanation.claims == []


def test_a_tool_result_the_model_cited_is_kept_even_without_a_number() -> None:
    events = generate_match("m02")
    info = get_match_info("m02")
    as_of = _as_of_at(events, 60)
    snapshot = match_snapshot(events, as_of=as_of)
    tool_fact = Fact(
        fact_id="key_moments:test:pressing_note",
        label="Merehaven Athletic pressing note",
        value="higher",
        event_ids=[],
    )
    results = [*snapshot, ToolResult(tool="key_moments", facts=[tool_fact])]
    snapshot_ids = [fact.fact_id for result in snapshot for fact in result.facts]
    explanation = Explanation(
        answer="Merehaven Athletic are pressing higher.",
        fact_ids=[tool_fact.fact_id, *snapshot_ids],
    )
    report = check_evidence(explanation, events=events, as_of=as_of, tool_results=results, info=info)
    assert report.passed, report.failures

    narrowed, narrowed_report = _cite_used_facts(
        explanation,
        report,
        model_fact_ids=[tool_fact.fact_id],
        snapshot_fact_ids=snapshot_ids,
        results=results,
        events=events,
        as_of=as_of,
        info=info,
    )
    assert narrowed.fact_ids == [tool_fact.fact_id]
    assert narrowed_report.passed


# Recap: no xG decimals, winner-first headline

_DECIMAL = re.compile(r"\d\.\d{2,}")


def _recap_texts(recap) -> list[str]:
    return [
        recap.headline,
        recap.summary,
        recap.audio_script,
        *(moment.text for moment in recap.key_moments),
        recap.notice or "",
    ]


@pytest.mark.parametrize("mode", ["off", "cached"])
@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_fallback_and_cached_recaps_have_no_decimals_and_a_winner_first_headline(
    match_id: str, mode: str
) -> None:
    events = generate_match(match_id)
    info = get_match_info(match_id)
    pipeline = PunditPipeline(replace(_settings(), mode=mode), None)
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.evidence.passed, result.evidence.failures
    assert not any(_DECIMAL.search(text) for text in _recap_texts(result.recap))
    goals = [event for event in events if event.type == "goal"]
    home = sum(event.team == "home" for event in goals)
    phrase = full_time_score_phrase(info, f"{home}-{len(goals) - home}")
    assert phrase in result.recap.headline
    assert "consolation" not in result.recap.summary
    assert "Verified match recap" not in result.recap.headline
    assert result.source == ("cached" if mode == "cached" else "fallback")


def test_the_recap_prompt_carries_no_xg_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")
    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [_recap_following_the_prompt])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    prompt = json.dumps(prompts[0])
    assert '"unit": "xG"' not in prompt
    assert not _DECIMAL.search(json.dumps(prompts[0]["facts"]))
    labels = [item["label"] for item in prompts[0]["facts"]]
    assert any("expected goals, rounded" in label for label in labels)
    assert any("chance quality" in label for label in labels)


def test_a_recap_with_an_xg_decimal_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def with_decimal(prompt: dict) -> RecapReply:
        reply = _recap_following_the_prompt(prompt)
        return reply.model_copy(
            update={"summary": reply.summary + " Merehaven Athletic had 3.919 xG."}
        )

    pipeline, prompts, _ = _scripted_pipeline(
        monkeypatch, [with_decimal, _recap_following_the_prompt]
    )
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 2
    assert "xG decimals" in prompts[1]["retry_feedback"]
    assert not any(_DECIMAL.search(text) for text in _recap_texts(result.recap))


def test_a_rounded_expected_goals_figure_is_grounded(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def rounded(prompt: dict) -> RecapReply:
        reply = _recap_following_the_prompt(prompt)
        fact = _alias(prompt, "Merehaven Athletic expected goals, rounded")
        return reply.model_copy(
            update={
                "summary": reply.summary
                + f" Merehaven Athletic had about {fact['value']} expected goals."
            }
        )

    pipeline, _, _ = _scripted_pipeline(monkeypatch, [rounded])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))
    assert result.source == "agent", result.evidence.failures
    assert "about 4 expected goals" in result.recap.summary


def test_a_recap_headline_without_the_score_phrase_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m02")
    info = get_match_info("m02")

    def no_score(prompt: dict) -> RecapReply:
        return _recap_following_the_prompt(prompt).model_copy(
            update={"headline": "Merehaven rally late to seize the points"}
        )

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [no_score, _recap_following_the_prompt])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 2
    assert "Merehaven Athletic win 2-1" in prompts[1]["retry_feedback"]
    assert "Merehaven Athletic win 2-1" in result.recap.headline


# Timing: no line before its event, and pause holds the viewer stream at once


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_resume_pushes_unreleased_events_back_by_the_pause() -> None:
    events = generate_match("m01")[:2]
    clock = _Clock()
    delay = BroadcastDelay(10, clock=clock)
    delay.stamp(events[0])  # seen at 100, due at 110
    clock.now = 104
    delay.pause()
    clock.now = 106
    delay.stamp(events[1])  # arrives during the pause
    clock.now = 109
    delay.resume()  # paused for 5 s

    assert delay.release_at(events[0].seq) == 115
    # It waits only for the 3 s of the pause after it arrived.
    assert delay.release_at(events[1].seq) == 106 + 10 + 3
    assert not delay.paused


def test_no_viewer_event_is_released_while_paused() -> None:
    events = generate_match("m01")[:6]

    async def run() -> tuple[list[int], list[int]]:
        delay = BroadcastDelay(0.5)
        released: list[int] = []

        async def source() -> AsyncIterator[Event]:
            for event in events:
                yield event

        async def consume() -> None:
            async for event in delay.events(source()):
                released.append(event.seq)

        task = asyncio.create_task(consume())
        # Pause once every event is stamped, well inside the 0.5 s delay (none released yet).
        async with asyncio.timeout(10):
            while len(delay._seen) < len(events):
                await asyncio.sleep(0.005)
        delay.pause()
        await asyncio.sleep(1.0)  # twice the delay: nothing may come out while paused
        during_pause = list(released)
        delay.resume()
        await asyncio.wait_for(task, timeout=10)
        return during_pause, released

    during_pause, released = asyncio.run(run())
    assert during_pause == []
    assert released == [event.seq for event in events]


def test_a_line_waits_while_the_broadcast_is_paused() -> None:
    event = generate_match("m01")[0]

    async def run() -> bool:
        delay = BroadcastDelay(0)
        delay.stamp(event)
        delay.pause()
        waiter = asyncio.create_task(delay.wait_released(event.seq))
        await asyncio.sleep(0.2)
        held = not waiter.done()
        delay.resume()
        await asyncio.wait_for(waiter, timeout=10)
        return held

    assert asyncio.run(run())


def test_a_line_never_reaches_the_viewer_before_its_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The viewer loop is slowed down, so its release lags the dispatcher's own wait."""
    original = BroadcastDelay.events

    async def slow_events(self, source):
        async for event in original(self, source):
            await asyncio.sleep(0.3)
            yield event

    monkeypatch.setattr(BroadcastDelay, "events", slow_events)
    received = _run_line_audio(_Synth(), monkeypatch)
    names = [name for name, _ in received]
    goal_index = next(
        index
        for index, (name, payload) in enumerate(received)
        if name == "event" and payload["type"] == "goal"
    )
    assert names.index("commentary") > goal_index


def test_pause_control_pauses_the_broadcast_delay() -> None:
    events = generate_match("m02")
    app = create_app(settings=_api_settings())
    with TestClient(app) as client:
        session = ReplaySession(
            replay_id="pause-replay",
            client_id="t",
            client_ip="testclient",  # TestClient's address
            info=get_match_info("m02"),
            events=events,
            replay=Replay(events, 20),
            delay=BroadcastDelay(10),
        )
        session.replay.state = "running"  # control only needs the state, not a publisher
        app.state.sessions[session.replay_id] = session
        token = {"X-Replay-Token": session.token}
        paused = client.post(
            "/api/replays/pause-replay/control", json={"action": "pause"}, headers=token
        )
        assert paused.json()["state"] == "paused"
        assert session.delay.paused
        resumed = client.post(
            "/api/replays/pause-replay/control", json={"action": "resume"}, headers=token
        )
        assert resumed.json()["state"] == "running"
        assert not session.delay.paused
        app.state.sessions.pop(session.replay_id)
