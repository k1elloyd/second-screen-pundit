import pytest
from pydantic import ValidationError

from second_screen_pundit.agents.commentator import (
    CommentaryLine,
    build_console_insight,
    validate_plain_spoken_text,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info


def test_console_insight_has_deterministic_facts_and_event_references() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    goal = next(
        event
        for event in events
        if event.type == "goal" and event.minute == 74
    )
    insight = build_console_insight(goal, info, events)

    assert insight.insight_id == f"m01-i{goal.seq:04d}"
    assert insight.event_ids[0] == goal.event_id
    assert insight.fact_ids
    score = next(fact for fact in insight.facts if fact["label"] == "Score")
    assert score["value"] == "1-2"
    assert goal.event_id in score["event_ids"]


def test_substitution_context_uses_player_names_not_ids() -> None:
    events = generate_match("m01")
    info = get_match_info("m01")
    substitution = next(event for event in events if event.type == "substitution")
    insight = build_console_insight(substitution, info, events)
    detail = insight.event_context["detail"]

    assert "player_on_name" in detail
    assert "player_off_name" in detail
    assert "player_on" not in detail
    assert "player_off" not in detail
    assert "-p" not in str(insight.event_context)


def test_commentary_line_is_structured_and_bounded() -> None:
    line = CommentaryLine(
        line_id="m01-l042",
        text="A measured finish gives the home side a route back into the match.",
        insight_id="m01-i0042",
        fact_ids=["match_state:score"],
        event_ids=["m01-e0042"],
        tone="measured",
    )
    assert len(line.text) <= 200

    with pytest.raises(ValidationError):
        CommentaryLine(
            line_id="m01-l042",
            text="x" * 201,
            insight_id="m01-i0042",
            fact_ids=[],
            event_ids=["m01-e0042"],
            tone="measured",
        )
    with pytest.raises(ValidationError):
        CommentaryLine(
            line_id="m01-l042",
            text="A line.",
            insight_id="m01-i0042",
            fact_ids=[],
            event_ids=["m01-e0042"],
            tone="overexcited",
        )


@pytest.mark.parametrize(
    "text",
    [
        "The goal stands. [m01-e0395]",
        "The score changes at m01-e0395.",
        "The score is supported by match_state:score.",
        "A citation marker is not spoken: [fact id].",
    ],
)
def test_plain_spoken_text_rejects_inline_citations(text: str) -> None:
    with pytest.raises(ValueError, match="plain spoken English"):
        validate_plain_spoken_text(text)


def test_plain_spoken_text_accepts_uncited_commentary() -> None:
    validate_plain_spoken_text("The home side find their way back into the match.")
