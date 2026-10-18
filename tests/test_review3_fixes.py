"""Review 3, group 2: viewer-facing checks, insight timing, question limits, repeated scores."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from second_screen_pundit.agents.content_filter import restricted_content_failures
from second_screen_pundit.agents.dispatcher import LiveDispatcher, WindowOutcome
from second_screen_pundit.agents.models import CommentaryLine, Insight, Recap, RecapMoment
from second_screen_pundit.agents.pipeline import (
    LiveWindowResult,
    PunditPipeline,
    recap_wording_failures,
)
from second_screen_pundit.agents.wording import banned_phrase_hits, strip_repeated_score
from second_screen_pundit.api import QuestionRateLimiter, create_app
from second_screen_pundit.engine.generate import generate_match, get_match_info
from tests.test_api import _denylist, _session, _settings
from tests.test_polish_wording import _first, _scripted
from tests.test_review2_fixes import _recap_following_the_prompt, _scripted_pipeline
from tests.test_stream_resume import _TOKENS, _Connection, _faster_clock, _scope, _stream

# A fictional name added to the hashed denylist for these tests only.
BLOCKED = "glimmerton wanderers"


# Item 3: the real-name check on live lines and the recap; banned phrases on the recap


def test_a_live_line_naming_a_denylisted_club_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _denylist(monkeypatch, BLOCKED)
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, _ = _scripted(
        monkeypatch, lambda p: f"Pressing like Glimmerton Wanderers. {p['score_phrase']}."
    )
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))
    assert result.line is not None and result.line.source == "fallback"
    assert "glimmerton" not in result.line.text.casefold()
    assert any("restricted content" in failure for failure in result.wording_failures)


def test_restricted_content_failures_names_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _denylist(monkeypatch, BLOCKED)
    assert restricted_content_failures("Port Calder press well.") == []
    assert restricted_content_failures("Just like Glimmerton Wanderers'.") == [
        "restricted content: a blocked real-club name"
    ]
    assert restricted_content_failures("Who are the bookmakers backing?") == [
        "restricted content: betting language"
    ]


def _recap(**text: str) -> Recap:
    moment = RecapMoment(minute=10, text=text.get("moment", "A goal."), event_ids=["m01-e0001"])
    return Recap(
        headline=text.get("headline", "Ashbridge Rovers win 3-2"),
        summary=text.get("summary", "Ashbridge Rovers came from behind."),
        key_moments=[moment, moment, moment],
        audio_script=text.get("audio", "Full time."),
    )


def test_recap_wording_failures_cover_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _denylist(monkeypatch, BLOCKED)
    info = get_match_info("m01")
    goals = ["away", "away", "home", "home", "home"]
    assert recap_wording_failures(_recap(), info, goals) == []
    # Banned only live, where the result isn't known: fine at full time.
    assert recap_wording_failures(_recap(summary="The winner came late on."), info, goals) == []
    assert recap_wording_failures(_recap(audio="An incredible finish."), info, goals) == [
        "audio_script: banned phrase hype: incredible"
    ]
    assert recap_wording_failures(_recap(moment="The crowd roared."), info, goals)[0].startswith(
        "key moment 1: banned phrase invented detail: the crowd"
    )
    assert recap_wording_failures(
        _recap(headline="Ashbridge Rovers win 3-2 like Glimmerton Wanderers"), info, goals
    ) == ["headline: restricted content: a blocked real-club name"]


def test_a_recap_with_a_banned_phrase_is_retried_with_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = generate_match("m01")
    info = get_match_info("m01")

    def hype(prompt: dict) -> Any:
        reply = _recap_following_the_prompt(prompt)
        return reply.model_copy(update={"summary": reply.summary + " An incredible finish."})

    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [hype, _recap_following_the_prompt])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "agent", result.evidence.failures
    assert result.attempts == 2
    assert "banned phrase hype: incredible" in prompts[1]["retry_feedback"]
    assert "incredible" not in result.recap.summary


def test_a_recap_naming_a_denylisted_club_twice_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _denylist(monkeypatch, BLOCKED)
    events = generate_match("m01")
    info = get_match_info("m01")

    def named(prompt: dict) -> Any:
        reply = _recap_following_the_prompt(prompt)
        return reply.model_copy(
            update={"audio_script": reply.audio_script + " Shades of Glimmerton Wanderers."}
        )

    pipeline, _, _ = _scripted_pipeline(monkeypatch, [named, named])
    result = asyncio.run(pipeline.produce_recap(info, events, as_of=events[-1].seq))

    assert result.source == "fallback"
    assert "glimmerton" not in result.recap.audio_script.casefold()


def test_the_away_side_and_the_home_side_are_banned_openers() -> None:
    info = get_match_info("m01")
    assert banned_phrase_hits("The away side press high.", info, []) == ["opener: the away side"]
    assert banned_phrase_hits("The home side sit deep.", info, []) == ["opener: the home side"]
    # Only as an opener.
    assert banned_phrase_hits("Port Calder are the away side.", info, []) == []


# Item 4: an insight never reaches the viewer before its event


class _InsightPipeline:
    """Every window yields an insight and no line, as a declined tactical window can."""

    async def comment_on_window(self, info, _events, window, *, as_of: int, **_kwargs):
        event = next((item for item in window if item.seq == as_of), window[-1])
        insight = Insight(
            insight_id=f"{info.match_id}-i{as_of}",
            minute=event.minute,
            headline="A goal changes the shape of the match.",
            importance=0.9,
            event_ids=[event.event_id],
        )
        return LiveWindowResult("skipped", None, insight, (), None)


def test_an_insight_is_sent_only_after_its_event_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _faster_clock(monkeypatch, 10)
    settings = replace(_settings(delay=0.5), mode="cached")
    app = create_app(settings=settings, pipeline=_InsightPipeline())  # type: ignore[arg-type]
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    async def run() -> list[tuple[int, str, dict]]:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.160"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120
            )
            _TOKENS[started["replay_id"]] = started["replay_token"]
            connection = _Connection(await _stream(routes, ip, started["replay_id"]))
            messages = await connection.read(
                lambda got: sum(name == "insight" for _, name, _ in got) >= 3, timeout=30
            )
            await connection.drop()
            return messages

    messages = asyncio.run(asyncio.wait_for(run(), 40))
    released: set[str] = set()
    insights = 0
    for _, name, payload in messages:
        if name == "event":
            released.add(payload["event_id"])
        elif name == "insight":
            insights += 1
            assert payload["event_id"] in released, "an insight arrived before its event"
            assert set(payload["insight"]["event_ids"]) <= released
    assert insights >= 3


# Item 5: the question limit is per viewer, inside a per-address ceiling


def test_two_viewers_on_one_address_each_get_their_own_question_limit() -> None:
    limiter = QuestionRateLimiter(limit=6, window_seconds=60, address_limit=20)
    for _ in range(6):
        limiter.check("198.51.100.170", client_id="judge-a", now=0)
        limiter.check("198.51.100.170", client_id="judge-b", now=0)
    with pytest.raises(HTTPException) as refused:
        limiter.check("198.51.100.170", client_id="judge-a", now=1)
    assert refused.value.status_code == 429
    assert "6 questions per minute" in refused.value.detail
    # A third viewer on the same address is unaffected by the first two.
    limiter.check("198.51.100.170", client_id="judge-c", now=1)


def test_rotating_client_ids_meets_the_address_ceiling() -> None:
    limiter = QuestionRateLimiter(limit=6, window_seconds=60, address_limit=20)
    for index in range(20):
        limiter.check("198.51.100.171", client_id=f"viewer-{index}", now=0)
    with pytest.raises(HTTPException) as refused:
        limiter.check("198.51.100.171", client_id="viewer-new", now=1)
    assert refused.value.status_code == 429
    assert "network" in refused.value.detail
    # A refused question uses up nothing: the new viewer's own limit is untouched.
    for _ in range(6):
        limiter.check("198.51.100.172", client_id="viewer-new", now=1)
    # The window passes and the address can ask again.
    limiter.check("198.51.100.171", client_id="viewer-new", now=61)


def test_ask_uses_the_replays_viewer_for_the_limit() -> None:
    settings = replace(_settings(), questions_per_minute=2, questions_per_address_per_minute=5)
    app = create_app(settings=settings, pipeline=PunditPipeline(settings, None))
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")

    def ask(client: TestClient, session) -> int:
        return client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Who is on top?"},
            headers={"X-Forwarded-For": "198.51.100.173", "X-Replay-Token": session.token},
        ).status_code

    with TestClient(app) as client:
        first = _session("limit-a", goal_seq=goal.seq, released_as_of=goal.seq)
        second = replace(
            _session("limit-b", goal_seq=goal.seq, released_as_of=goal.seq), client_id="other"
        )
        for session in (first, second):
            app.state.sessions[session.replay_id] = session
        assert [ask(client, first) for _ in range(3)] == [200, 200, 429]
        # Same address, another viewer: their own two questions.
        assert [ask(client, second) for _ in range(3)] == [200, 200, 429]


# Item 7: a score the other lane has just given isn't repeated


def test_strip_repeated_score_drops_only_a_trailing_repeat() -> None:
    recent = ["Ashbridge Rovers pull one back! Port Calder FC lead 2-1."]
    assert (
        strip_repeated_score("Ashbridge Rovers push higher. Port Calder lead 2-1.", recent)
        == "Ashbridge Rovers push higher."
    )
    # Either way round is the same score.
    assert (
        strip_repeated_score("Ashbridge press on. Ashbridge Rovers trail 1-2.", recent)
        == "Ashbridge press on."
    )
    # A different score, a score inside a sentence, or a one-sentence line: unchanged.
    assert strip_repeated_score("Ashbridge press. Port Calder lead 3-1.", recent).endswith("3-1.")
    kept = "Port Calder sit deep at 2-1 and wait."
    assert strip_repeated_score(kept, recent) == kept
    assert strip_repeated_score("Port Calder lead 2-1.", recent) == "Port Calder lead 2-1."
    assert strip_repeated_score("Ashbridge press. Port Calder lead 2-1.", []).endswith("2-1.")


class _QueuedPipeline:
    def __init__(self, lines: list[CommentaryLine]) -> None:
        self.lines = lines

    async def comment_on_window(self, *_args, **_kwargs) -> LiveWindowResult:
        return LiveWindowResult("commentary", self.lines.pop(0), None, (), None)


def _line(seq: int, text: str, tier: str) -> CommentaryLine:
    return CommentaryLine(
        line_id=f"m01-l{seq:03d}",
        text=text,
        insight_id=f"m01-i{seq}",
        tone="measured",
        delivery_tier=tier,  # type: ignore[arg-type]
        event_ids=[f"m01-e{seq:04d}"],
    )


def _publish(monkeypatch: pytest.MonkeyPatch, lines: list[CommentaryLine], *, window: float):
    import second_screen_pundit.agents.dispatcher as dispatcher_module

    monkeypatch.setattr(dispatcher_module, "REPEATED_SCORE_SECONDS", window)
    events = generate_match("m01")
    dispatcher = LiveDispatcher(
        _QueuedPipeline(lines), get_match_info("m01"), events, _settings()  # type: ignore[arg-type]
    )

    async def run() -> list[str]:
        for index, event in enumerate(events[100:100 + len(lines)]):
            path = "fast_path" if index == 0 else "tactical"
            outcome = WindowOutcome(path, event, event.minute)  # type: ignore[arg-type]
            await dispatcher._run(outcome, [event], (), None)
        return [outcome.result.line.text for outcome in dispatcher.outcomes]  # type: ignore[union-attr]

    return asyncio.run(run())


def test_a_tactical_line_does_not_repeat_the_score_the_goal_line_just_gave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        _line(1, "Kestrelane scores for Port Calder! Port Calder FC lead 2-1.", "goal"),
        _line(2, "Port Calder have the better of the chances. Port Calder lead 2-1.", "tactical"),
    ]
    published = _publish(monkeypatch, lines, window=30)
    assert published[0].endswith("lead 2-1.")  # the goal line keeps its score
    assert published[1] == "Port Calder have the better of the chances."


def test_a_goal_line_keeps_its_score_and_an_old_score_line_is_not_a_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        _line(1, "Port Calder have more of the ball. Port Calder FC lead 2-1.", "tactical"),
        _line(2, "Kestrelane again! Port Calder FC lead 2-1.", "goal"),
    ]
    assert _publish(monkeypatch, lines, window=30)[1].endswith("lead 2-1.")
    # Outside the window (0 s here), a repeat is kept.
    lines = [
        _line(1, "Kestrelane scores for Port Calder! Port Calder FC lead 2-1.", "goal"),
        _line(2, "Port Calder have the better of the chances. Port Calder lead 2-1.", "tactical"),
    ]
    assert _publish(monkeypatch, lines, window=-1)[1].endswith("lead 2-1.")
