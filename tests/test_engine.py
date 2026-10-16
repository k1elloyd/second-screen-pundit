import hashlib
import re

import pytest
from pydantic import ValidationError

from second_screen_pundit.engine.generate import (
    generate_match,
    get_match_info,
    load_squads,
    read_event_log,
    write_event_log,
)
from second_screen_pundit.engine.schema import Event


REAL_CLUB_NAME_FINGERPRINTS = {
    "b93b9776163702f1fad6cbaf815326a41b3285d0961f4e838ebdb8ad52e5f16e",
    "9e861941ad8bf5bcb649e5fde92d712528200a216018c2437371498e6ab7683d",
    "15c6d611193988e468c7431229c59ce13b0407fba24f11d36c42680d7fa11e98",
    "1ecd41c03ef78bd6daeaa6bb008896607a8413bf8ba6266be80327554b370a9e",
    "cd111a88b18961c729263aa10f90848935da373f7480179c402427de838141c7",
    "c762f2e9d54d8014de25fba5bd49265b5e75d504c514be6aac08ead41b71c72a",
    "7c8159dd2e49a576176f5461ea9d8919abdfd0ed4e29e2b4c1c7b98a8060bf40",
}


def _score_at(events: list[Event], minute: int) -> tuple[int, int]:
    goals = [
        event
        for event in events
        if event.type == "goal" and event.minute + event.added_minute <= minute
    ]
    return (
        sum(event.team == "home" for event in goals),
        sum(event.team == "away" for event in goals),
    )


def test_event_schema_validates_identifiers_clock_and_coordinates() -> None:
    valid = Event(
        event_id="m01-e0001",
        match_id="m01",
        seq=1,
        period=1,
        minute=10,
        second=5,
        team="home",
        player_id="ashbridge-p02",
        type="pass",
        x=12,
        y=30,
        end_x=44,
        end_y=31,
        outcome="success",
        detail={"receiver_id": "ashbridge-p06"},
    )
    assert valid.event_id == "m01-e0001"

    with pytest.raises(ValidationError, match="event_id must be"):
        Event.model_validate({**valid.model_dump(), "event_id": "m01-e0002"})
    with pytest.raises(ValidationError, match="require start and end coordinates"):
        Event(
            event_id="m01-e0001",
            match_id="m01",
            seq=1,
            period=1,
            minute=10,
            second=5,
            team="home",
            type="pass",
            x=12,
            y=30,
        )
    with pytest.raises(ValidationError):
        Event.model_validate({**valid.model_dump(), "x": 101})


def test_seeded_generator_is_repeatable_and_has_expected_event_rate() -> None:
    first = generate_match("m01")
    second = generate_match("m01")

    assert [event.model_dump() for event in first] == [
        event.model_dump() for event in second
    ]
    assert 1500 <= len(first) <= 2000
    assert [event.seq for event in first] == list(range(1, len(first) + 1))
    assert [event.event_id for event in first] == [
        f"m01-e{seq:04d}" for seq in range(1, len(first) + 1)
    ]


def test_m01_scripted_score_and_back_three() -> None:
    events = generate_match("m01")
    assert _score_at(events, 70) == (0, 2)
    assert _score_at(events, 90) == (3, 2)
    home_formations = [
        event.detail["formation"]
        for event in events
        if event.team == "home" and event.type == "formation_change"
    ]
    assert home_formations[-1] == "3-4-3"


def test_m02_red_card_and_m03_high_turnovers_are_scripted() -> None:
    m02 = generate_match("m02")
    red_cards = [
        event
        for event in m02
        if event.type == "card" and event.detail.get("colour") == "red"
    ]
    assert [(event.team, event.minute) for event in red_cards] == [("home", 38)]
    assert any(
        event.team == "home"
        and event.type == "formation_change"
        and event.minute == 39
        and event.detail["formation"] == "4-4-1"
        for event in m02
    )
    assert sum(
        event.team == "away"
        and event.type == "pass"
        and event.x is not None
        and event.x >= 68
        for event in m02
    ) >= 16

    m03 = generate_match("m03")
    goals = [event for event in m03 if event.type == "goal"]
    away_xg = sum(event.xg or 0 for event in m03 if event.type == "shot" and event.team == "away")
    scripted_turnovers = [
        event
        for event in m03
        if event.team == "home"
        and event.type == "interception"
        and event.outcome == "success"
        and event.x is not None
        and event.x >= 60
    ]
    assert len([goal for goal in goals if goal.team == "home"]) > len(
        [goal for goal in goals if goal.team == "away"]
    )
    assert away_xg < get_match_info("m03").away_xg_limit
    assert len(scripted_turnovers) >= 5
    turnover_ids = {event.event_id for event in scripted_turnovers}
    home_goals = [event for event in goals if event.team == "home"]
    assert all(
        any(event_id in turnover_ids for event_id in goal.related_event_ids)
        for goal in home_goals
    )


def test_squads_are_curated_and_do_not_match_the_hashed_denylist() -> None:
    squads = load_squads()
    assert len(squads) == 3
    all_names = [team.name for team in squads.values()]
    assert all(len(team.players) >= 11 for team in squads.values())

    for name in all_names:
        canonical = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert fingerprint not in REAL_CLUB_NAME_FINGERPRINTS


def test_event_log_jsonl_round_trip(tmp_path) -> None:
    events = generate_match("m01")[:20]
    path = tmp_path / "nested" / "m01.jsonl"
    write_event_log(events, path)

    assert read_event_log(path) == events
    assert len(path.read_text(encoding="utf-8").splitlines()) == len(events)
