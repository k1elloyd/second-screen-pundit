"""Phase 2c regression fixes: reply schema, logged validation errors, score rule, tidy text,
percentages, one line per formation change, and the default voice."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from second_screen_pundit.agents.clients import _MAX_TOKENS, _OUTPUT_MODELS
from second_screen_pundit.agents.commentator import live_line_id, tidy_spoken_text
from second_screen_pundit.agents.evidence import check_evidence, mentions_score
from second_screen_pundit.agents.fallbacks import FALLBACK_VARIANTS, fallback_lines
from second_screen_pundit.agents.models import CommentaryLine, CommentaryReply
from second_screen_pundit.agents.pipeline import (
    PunditPipeline,
    describe_validation_error,
    score_phrase_allowed,
)
from second_screen_pundit.agents.prompts import COMMENTATOR_INSTRUCTIONS
from second_screen_pundit.agents.speech import DEFAULT_FALLBACK_VOICE, DEFAULT_VOICE
from second_screen_pundit.agents.tools.analytics import Fact, ToolResult
from second_screen_pundit.engine.generate import generate_match, get_match_info
from tests.test_tactical import _dispatch, _StubPipeline
from tests.test_voice_and_style import _settings


def test_commentator_returns_only_text_and_citations() -> None:
    assert _OUTPUT_MODELS["commentator"] is CommentaryReply
    assert set(CommentaryReply.model_fields) == {"text", "fact_ids", "event_ids"}
    assert _MAX_TOKENS["commentator"] == 700


def test_commentator_instructions_keep_the_hard_rules_and_four_examples() -> None:
    examples = COMMENTATOR_INSTRUCTIONS.split("EXAMPLES.\n", 1)[1].split("\n")
    assert len(examples) == 4
    for section in ("FORM.", "NUMBERS.", "SCORE.", "GOALS.", "FORMATIONS.", "CONTENT."):
        assert section in COMMENTATOR_INSTRUCTIONS
    assert "VARIETY." not in COMMENTATOR_INSTRUCTIONS
    assert "delivery_tier" not in COMMENTATOR_INSTRUCTIONS


def test_validation_error_names_the_field_and_quotes_the_reply() -> None:
    with pytest.raises(ValidationError) as caught:
        CommentaryReply.model_validate_json('{"txt": "Port Calder lead 1-0", "fact_ids": []}')
    detail = describe_validation_error(caught.value, '{"txt": "Port Calder lead 1-0"}')
    assert "text: missing" in detail and "txt: extra_forbidden" in detail
    assert "Port Calder lead 1-0" in detail


def test_long_reply_excerpt_is_cut_short() -> None:
    with pytest.raises(ValidationError) as caught:
        CommentaryReply.model_validate_json('{"text": ' + json.dumps("x" * 400))
    detail = describe_validation_error(caught.value)
    assert detail.endswith("...'") and len(detail) < 300


def _goal(match_id: str = "m01", minute: int = 23):  # type: ignore[no-untyped-def]
    events = generate_match(match_id)
    return events, next(e for e in events if e.type == "goal" and e.minute == minute)


class _InvalidResponse:
    text = '{"txt": "Brio Fallowmere scores"}'
    finish_reason = "stop"
    usage_details = None

    @property
    def value(self) -> object:
        return CommentaryReply.model_validate_json(self.text)


def test_invalid_commentator_reply_is_recorded_with_its_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _goal()
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Agent:
        async def run(self, _: str) -> _InvalidResponse:
            return _InvalidResponse()

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Agent())
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))

    assert result.line is not None and result.line.source == "fallback"
    assert len(result.output_failures) == 2
    assert all(f.startswith("commentator: ") for f in result.output_failures)
    assert "text: missing" in result.output_failures[0]
    assert "Brio Fallowmere scores" in result.output_failures[0]
    assert [t.finish_reason for t in result.timings] == ["stop", "stop"]


def test_server_sets_line_id_insight_tone_and_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, goal = _goal()
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    supplied: list[dict[str, object]] = []

    class Agent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            supplied.append(payload)
            return SimpleNamespace(
                value=CommentaryReply(
                    text="Brio Fallowmere strikes first.\n\n  " + str(payload["score_phrase"]) + ".",
                    event_ids=payload["insight"]["event_ids"],  # type: ignore[index]
                    fact_ids=payload["insight"]["fact_ids"],  # type: ignore[index]
                )
            )

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Agent())
    result = asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))

    assert result.line is not None and result.line.source == "agent"
    assert result.line.text == "Brio Fallowmere strikes first. Port Calder lead 1-0."
    assert result.line.line_id == live_line_id("m01", goal.seq, "fast_path")
    assert result.line.tone == "urgent" and result.line.delivery_tier == "goal"
    assert "required_line_id" not in supplied[0]
    assert "insight_id" not in supplied[0]["insight"]  # type: ignore[operator]


def test_over_long_reply_is_retried_with_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, goal = _goal()
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    feedback: list[str] = []

    class Agent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            feedback.append(str(payload["retry_feedback"]))
            return SimpleNamespace(
                value=CommentaryReply(text="Long " * 60, event_ids=payload["insight"]["event_ids"])  # type: ignore[index]
            )

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Agent())
    asyncio.run(pipeline.comment_on_window(info, events, [goal], as_of=goal.seq))

    assert feedback[1].startswith("text is ") and "the limit is 200" in feedback[1]


def test_tidy_text_collapses_whitespace_and_newlines() -> None:
    assert tidy_spoken_text("Sent off.\nPort Calder  lead 1-0 ") == "Sent off. Port Calder lead 1-0"


@pytest.mark.parametrize(
    ("recent", "goal_or_red", "allowed"),
    [
        ((), False, True),
        (("Port Calder lead 1-0.",), False, False),
        (("Port Calder lead 1-0.", "a", "b", "c"), False, True),
        (("Port Calder lead 1-0.",), True, True),
        (("Port Calder lead 2-0.",), False, True),
    ],
)
def test_score_phrase_only_on_goals_red_cards_or_when_not_said_recently(
    recent: tuple[str, ...], goal_or_red: bool, allowed: bool
) -> None:
    assert score_phrase_allowed(
        "Port Calder lead 1-0", recent, goal_or_red_card=goal_or_red
    ) is allowed


def test_formation_is_not_a_score() -> None:
    assert mentions_score("Port Calder lead 1-0.")
    assert not mentions_score("Port Calder have changed to a 4-4-1 shape.")
    assert not mentions_score("Ashbridge Rovers go to a 3-4-3.")


def test_line_stating_the_score_fails_when_the_score_was_withheld() -> None:
    info = get_match_info("m01")
    events, goal = _goal()
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    results = [ToolResult(tool="match_state", facts=[
        Fact(fact_id="match_state:score", label="Match-level score at 30'", value="1-0",
             unit=None, event_ids=[goal.event_id]),
    ])]
    from second_screen_pundit.agents.models import Insight

    insight = Insight(insight_id="i", minute=30, headline="h", importance=1,
                      fact_ids=["match_state:score"], event_ids=[goal.event_id])
    line = CommentaryLine(line_id=live_line_id("m01", goal.seq, "tactical"),
                          text="Port Calder lead 1-0.", insight_id="i", tone="analytical",
                          fact_ids=["match_state:score"], event_ids=[goal.event_id])
    kwargs = dict(info=info, events=events, as_of=goal.seq, tool_results=results, lane="tactical")
    assert pipeline._check_line(line, insight, **kwargs).passed  # type: ignore[arg-type]
    refused = pipeline._check_line(line, insight, score_allowed=False, **kwargs)  # type: ignore[arg-type]
    assert not refused.passed and "must not state the score" in refused.failures[0]


def test_fallbacks_drop_the_score_sentence_when_it_is_withheld() -> None:
    values = {"team": "Port Calder", "formation": "4-4-1", "score_phrase": None}
    lines = list(fallback_lines("formation_change", values, line_id="m02-l0700-fast_path"))
    assert lines and all("lead" not in line and line.endswith(".") for line in lines)
    assert len(lines) == len(FALLBACK_VARIANTS["formation_change"]) - 1  # one needs previous_formation


def _possession_line(text: str) -> tuple[CommentaryLine, list[ToolResult]]:
    fact = Fact(fact_id="tactical_signals:possession_21_after", label="Ashbridge Rovers possession, 11-21' at 21'",
                value=52.7, unit="%", spoken_value=53, event_ids=["m01-e0343"])
    line = CommentaryLine(line_id="l", text=text, insight_id="i", tone="analytical",
                          fact_ids=[fact.fact_id], event_ids=["m01-e0343"])
    return line, [ToolResult(tool="tactical_signals", facts=[fact])]


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("Ashbridge Rovers have 53 possession now.", False),
        ("Ashbridge Rovers have 53% of the possession now.", True),
        ("Ashbridge Rovers have 53 per cent of the possession now.", True),
        ("Ashbridge Rovers have 52.7% of the possession now.", True),
    ],
)
def test_percentages_must_be_spoken_as_percentages(text: str, passed: bool) -> None:
    line, results = _possession_line(text)
    report = check_evidence(line, events=generate_match("m01"), as_of=400,
                            tool_results=results, info=get_match_info("m01"))
    assert report.passed is passed, report.failures
    if not passed:
        assert "must be spoken with % or per cent" in report.failures[0]


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_each_formation_change_gets_one_line_across_both_paths(match_id: str) -> None:
    dispatcher = _dispatch(match_id, _StubPipeline())
    changes = [e for e in generate_match(match_id) if e.type == "formation_change"]
    for change in changes:
        covering = [
            outcome
            for outcome in dispatcher.outcomes
            if outcome.event.seq == change.seq
            or any(signal.key == f"formation_{change.seq}" for signal in outcome.signals)
        ]
        assert len(covering) == 1, (match_id, change.minute, [o.path for o in covering])


def test_fast_path_formation_line_can_name_the_previous_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    change = next(e for e in events if e.type == "formation_change" and e.detail["formation"] == "3-4-3")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class Failing:
        async def run(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(value=None, text="")

    monkeypatch.setattr(pipeline, "_agent", lambda *_a, **_k: Failing())
    result = asyncio.run(pipeline.comment_on_window(info, events, [change], as_of=change.seq))
    facts = {fact.fact_id: fact for r in result.tool_results for fact in r.facts}
    assert facts[f"formation_at:{change.seq}:previous_formation"].value == "4-2-3-1"
    assert result.evidence is not None and result.evidence.passed


def test_default_voice_is_harry_with_ryan_as_fallback() -> None:
    assert DEFAULT_VOICE == "en-GB-Harry:MAI-Voice-2.1-Flash"
    assert DEFAULT_FALLBACK_VOICE == "en-GB-RyanNeural"
