import pytest
from pydantic import ValidationError

from second_screen_pundit.agents.tools.analytics import (
    ToolResult,
    compare_windows,
    formation_at,
    invoke_tool,
    key_moments,
    match_state,
    pass_network,
    possession_window,
    pressing_intensity,
    shot_summary,
    xg_timeline,
)
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.models import Insight
from second_screen_pundit.agents.tools.parameters import (
    CompareWindowsParams,
    FormationAtParams,
    KeyMomentsParams,
    MatchStateParams,
    MinuteRange,
    PassNetworkParams,
    PossessionWindowParams,
    PressingIntensityParams,
    ShotSummaryParams,
    XgTimelineParams,
)
from second_screen_pundit.engine.schema import Event


def _event(
    seq: int,
    minute: int,
    event_type: str,
    team: str | None,
    **values,
) -> Event:
    return Event(
        event_id=f"m01-e{seq:04d}",
        match_id="m01",
        seq=seq,
        period=1 if minute <= 45 else 2,
        minute=minute,
        second=0,
        team=team,
        type=event_type,
        **values,
    )


@pytest.fixture
def fixture_events() -> list[Event]:
    return [
        _event(1, 0, "kickoff", "home", detail={"formation": "4-3-3"}),
        _event(2, 0, "kickoff", "away", detail={"formation": "4-2-3-1"}),
        _event(
            3,
            10,
            "pass",
            "home",
            player_id="home-p01",
            x=10,
            y=20,
            end_x=40,
            end_y=30,
            outcome="success",
            detail={"receiver_id": "home-p02"},
        ),
        _event(
            4,
            12,
            "pass",
            "away",
            player_id="away-p01",
            x=20,
            y=30,
            end_x=30,
            end_y=35,
            outcome="success",
            detail={"receiver_id": "away-p02"},
        ),
        _event(
            5,
            14,
            "pass",
            "home",
            player_id="home-p01",
            x=10,
            y=20,
            end_x=80,
            end_y=30,
            outcome="fail",
            detail={},
        ),
        _event(6, 15, "tackle", "home", x=55, y=50, outcome="success"),
        _event(7, 16, "interception", "home", x=65, y=45, outcome="success"),
        _event(8, 17, "foul", "away", x=75, y=60),
        _event(
            9,
            20,
            "shot",
            "home",
            player_id="home-p03",
            x=88,
            y=50,
            outcome="success",
            xg=0.3,
        ),
        _event(
            10,
            20,
            "goal",
            "home",
            player_id="home-p03",
            related_event_ids=("m01-e0009",),
        ),
        _event(
            11,
            21,
            "shot",
            "away",
            player_id="away-p03",
            x=82,
            y=48,
            outcome="fail",
            xg=0.25,
        ),
        _event(
            12,
            30,
            "formation_change",
            "home",
            detail={"formation": "3-4-3"},
        ),
        _event(13, 31, "card", "away", detail={"colour": "yellow"}),
    ]


def _value(result: ToolResult, label: str):
    return next(fact.value for fact in result.facts if fact.label == label)


def test_match_state_computes_score_possession_shots_xg_cards_and_formations(
    fixture_events: list[Event],
) -> None:
    result = match_state(
        fixture_events,
        MatchStateParams(minute=90),
        as_of=13,
    )

    assert _value(result, "Score") == "1-0"
    assert _value(result, "Home possession") == 50
    assert _value(result, "Shots") == 2
    assert _value(result, "Home xG") == 0.3
    assert _value(result, "Away xG") == 0.25
    assert _value(result, "Home cards") == 0
    assert _value(result, "Away cards") == 1
    assert _value(result, "Home formation") == "3-4-3"
    assert _value(result, "Away formation") == "4-2-3-1"


def test_possession_window_and_xg_timeline(fixture_events: list[Event]) -> None:
    possession = possession_window(
        fixture_events,
        PossessionWindowParams(start=0, end=30),
        as_of=13,
    )
    assert _value(possession, "Home possession") == 50
    assert _value(possession, "Away possession") == 50

    timeline = xg_timeline(
        fixture_events,
        XgTimelineParams(),
        as_of=13,
    )
    assert timeline.table is not None
    assert timeline.table[3] == {"through_minute": 20, "home_xg": 0.3, "away_xg": 0.0}
    assert timeline.table[4] == {"through_minute": 25, "home_xg": 0.3, "away_xg": 0.25}


def test_shot_summary_uses_on_target_and_best_chance_definitions(
    fixture_events: list[Event],
) -> None:
    result = shot_summary(
        fixture_events,
        ShotSummaryParams(team="home", start=0, end=30),
        as_of=13,
    )
    assert _value(result, "Shots") == 1
    assert _value(result, "On target") == 1
    assert _value(result, "xG") == 0.3
    assert _value(result, "Best chance") == 0.3
    assert result.facts[-1].event_ids == ["m01-e0009"]


def test_pressing_and_pass_network_apply_thresholds_and_success_rules(
    fixture_events: list[Event],
) -> None:
    pressing = pressing_intensity(
        fixture_events,
        PressingIntensityParams(team="home", start=0, end=30),
        as_of=13,
    )
    assert _value(pressing, "Defensive actions per minute") == 0.07
    assert _value(pressing, "High turnovers") == 1

    network = pass_network(
        fixture_events,
        PassNetworkParams(team="home", start=0, end=30),
        as_of=13,
    )
    assert _value(network, "Progressive passes") == 1
    assert network.table == [
        {"passer_id": "home-p01", "receiver_id": "home-p02", "passes": 1}
    ]


def test_formation_and_key_moments_rank_in_specified_order(
    fixture_events: list[Event],
) -> None:
    formation = formation_at(
        fixture_events,
        FormationAtParams(team="home", minute=30),
        as_of=13,
    )
    assert _value(formation, "Home formation") == "3-4-3"
    assert formation.facts[0].event_ids == ["m01-e0012"]

    moments = key_moments(
        fixture_events,
        KeyMomentsParams(limit=10),
        as_of=13,
    )
    assert moments.table is not None
    assert [row["event_id"] for row in moments.table] == [
        "m01-e0010",
        "m01-e0012",
        "m01-e0009",
        "m01-e0013",
    ]


def test_compare_windows_and_as_of_clamping(fixture_events: list[Event]) -> None:
    comparison = compare_windows(
        fixture_events,
        CompareWindowsParams(
            team="home",
            metric="xg",
            before=MinuteRange(start=0, end=20),
            after=MinuteRange(start=20, end=30),
        ),
        as_of=13,
    )
    assert [_value(comparison, label) for label in ("Before", "After", "Change")] == [
        0,
        0.3,
        0.3,
    ]

    future = match_state(
        fixture_events,
        MatchStateParams(minute=90),
        as_of=9,
    )
    assert _value(future, "Score") == "0-0"
    assert any("clamped from 90 to 20" in caveat for caveat in future.caveats)
    future_shots = shot_summary(
        fixture_events,
        ShotSummaryParams(team="away", start=0, end=90),
        as_of=10,
    )
    assert _value(future_shots, "Shots") == 0
    assert any("clamped" in caveat for caveat in future_shots.caveats)


def test_tools_reject_unknown_parameters_and_invalid_choices(
    fixture_events: list[Event],
) -> None:
    with pytest.raises(ValidationError):
        MatchStateParams.model_validate({"minute": 30, "as_of": 8})
    with pytest.raises(ValidationError):
        CompareWindowsParams.model_validate(
            {
                "team": "home",
                "metric": "goals",
                "before": {"start": 0, "end": 10},
                "after": {"start": 10, "end": 20},
            }
        )
    with pytest.raises(ValueError, match="Unknown analytics tool"):
        invoke_tool(fixture_events, "not_a_tool", {}, as_of=13)
    result = invoke_tool(
        fixture_events,
        "match_state",
        {"minute": 30},
        as_of=13,
    )
    assert isinstance(result, ToolResult)


def test_tool_evidence_ids_are_capped_and_grounded_in_returned_facts() -> None:
    events = [
        _event(
            seq,
            seq,
            "pass",
            "home" if seq % 2 else "away",
            x=10,
            y=20,
            end_x=30,
            end_y=25,
            outcome="success",
            detail={"receiver_id": f"player-{seq}"},
        )
        for seq in range(1, 9)
    ]
    result = possession_window(
        events,
        PossessionWindowParams(start=0, end=10),
        as_of=8,
    )
    fact = next(item for item in result.facts if item.label == "Home possession")

    assert len(fact.event_ids) == 5
    assert fact.event_ids == [event.event_id for event in events[2:7]]
    assert len({event_id for item in result.facts for event_id in item.event_ids}) <= 5
    insight = Insight(
        insight_id="m01-i0008",
        minute=8,
        headline="Possession is shared",
        importance=0.7,
        fact_ids=[fact.fact_id],
        event_ids=fact.event_ids,
    )
    report = check_evidence(
        insight,
        events=events,
        as_of=8,
        tool_results=[result],
    )

    assert report.passed, report.failures
