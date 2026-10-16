from __future__ import annotations

import random
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from second_screen_pundit.engine.schema import Event, MatchInfo, MatchSquad


@dataclass(frozen=True)
class _Draft:
    clock: int
    order: int
    values: dict[str, Any]
    key: str | None = None
    related_keys: tuple[str, ...] = ()


def _read_yaml(filename: str) -> dict[str, Any]:
    resource = files("second_screen_pundit.engine").joinpath(filename)
    parsed = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{filename} must contain a YAML mapping")
    return parsed


def load_squads() -> dict[str, MatchSquad]:
    data = _read_yaml("squads.yaml")
    return {
        team["id"]: MatchSquad.model_validate(team)
        for team in data["teams"]
    }


def get_match_info(match_id: str) -> MatchInfo:
    storylines = _read_yaml("storylines.yaml")["storylines"]
    if match_id not in storylines:
        raise ValueError(f"Unknown match id: {match_id}")
    story = storylines[match_id]
    squads = load_squads()
    home = squads[story["home"]]
    away = squads[story["away"]]
    return MatchInfo(
        match_id=match_id,
        seed=story["seed"],
        home=home,
        away=away,
        stadium=story["stadium"],
        away_xg_limit=story.get("away_xg_limit"),
    )


def _event_values(
    *,
    minute: int,
    second: int,
    team: str | None,
    player_id: str | None,
    event_type: str,
    **values: Any,
) -> dict[str, Any]:
    return {
        "period": 1 if minute <= 45 else 2,
        "minute": minute,
        "added_minute": 0,
        "second": second,
        "team": team,
        "player_id": player_id,
        "type": event_type,
        **values,
    }


def _add_draft(
    drafts: list[_Draft],
    *,
    minute: int,
    second: int,
    team: str | None,
    player_id: str | None,
    event_type: str,
    key: str | None = None,
    related_keys: tuple[str, ...] = (),
    **values: Any,
) -> None:
    drafts.append(
        _Draft(
            clock=minute * 60 + second,
            order=len(drafts),
            values=_event_values(
                minute=minute,
                second=second,
                team=team,
                player_id=player_id,
                event_type=event_type,
                **values,
            ),
            key=key,
            related_keys=related_keys,
        )
    )


def _player_id(team: MatchSquad, shirt: int) -> str:
    return team.players[shirt - 1]["id"]


def _inject_beats(
    drafts: list[_Draft],
    story: dict[str, Any],
    home: MatchSquad,
    away: MatchSquad,
) -> None:
    sides = {"home": home, "away": away}
    last_high_turnover_key: str | None = None
    for beat_index, beat in enumerate(story["beats"], start=1):
        team = sides[beat["team"]]
        player_id = _player_id(team, beat.get("player", 1))
        minute = beat["minute"]
        kind = beat["kind"]
        prefix = f"script-{beat_index}"

        if kind == "goal":
            shot_key = f"{prefix}-shot"
            related_keys = (shot_key,)
            if story is not None and story.get("away_xg_limit") is not None and beat["team"] == "home":
                if last_high_turnover_key is not None:
                    related_keys = (shot_key, last_high_turnover_key)
            _add_draft(
                drafts,
                minute=minute,
                second=50,
                team=beat["team"],
                player_id=player_id,
                event_type="shot",
                key=shot_key,
                x=88.0,
                y=50.0,
                outcome="success",
                xg=beat["xg"],
            )
            _add_draft(
                drafts,
                minute=minute,
                second=55,
                team=beat["team"],
                player_id=player_id,
                event_type="goal",
                related_keys=related_keys,
                detail={},
            )
        elif kind == "red_card":
            _add_draft(
                drafts,
                minute=minute,
                second=55,
                team=beat["team"],
                player_id=player_id,
                event_type="card",
                detail={"colour": "red"},
            )
        elif kind == "formation":
            _add_draft(
                drafts,
                minute=minute,
                second=58,
                team=beat["team"],
                player_id=None,
                event_type="formation_change",
                detail={"formation": beat["formation"]},
            )
        elif kind == "substitution":
            _add_draft(
                drafts,
                minute=minute,
                second=55,
                team=beat["team"],
                player_id=player_id,
                event_type="substitution",
                detail={
                    "player_off": _player_id(team, beat["player_off"]),
                    "player_on": _player_id(team, beat["player_on"]),
                },
            )
        elif kind == "high_turnover":
            last_high_turnover_key = f"{prefix}-turnover"
            _add_draft(
                drafts,
                minute=minute,
                second=55,
                team=beat["team"],
                player_id=player_id,
                event_type="interception",
                key=last_high_turnover_key,
                x=68.0,
                y=42.0,
                outcome="success",
            )
        elif kind == "final_third":
            for offset in range(4):
                _add_draft(
                    drafts,
                    minute=minute,
                    second=50 + offset,
                    team=beat["team"],
                    player_id=_player_id(team, offset + 6),
                    event_type="pass",
                    x=68.0 + offset,
                    y=38.0 + offset * 4,
                    end_x=72.0 + offset,
                    end_y=40.0 + offset * 4,
                    outcome="success",
                    detail={
                        "receiver_id": _player_id(team, offset + 7),
                    },
                )
        else:
            raise ValueError(f"Unknown storyline beat kind: {kind}")


def _random_play(
    drafts: list[_Draft],
    info: MatchInfo,
    rng: random.Random,
) -> None:
    teams = {"home": info.home, "away": info.away}
    current_team = "home"
    for minute in range(1, 91):
        for action_index in range(17):
            if action_index and rng.random() < 0.19:
                current_team = "away" if current_team == "home" else "home"
            team = teams[current_team]
            player_index = rng.randrange(1, 12)
            player_id = _player_id(team, player_index)
            x = round(rng.uniform(2, 98), 1)
            y = round(rng.uniform(2, 98), 1)
            second = rng.randrange(0, 50)
            draw = rng.random()
            values: dict[str, Any] = {}
            if draw < 0.73:
                event_type = "pass"
                success = rng.random() < 0.79
                end_x = min(100.0, max(0.0, round(x + rng.uniform(-18, 24), 1)))
                values = {
                    "end_x": end_x,
                    "end_y": round(min(100.0, max(0.0, y + rng.uniform(-22, 22))), 1),
                    "outcome": "success" if success else "fail",
                    "detail": {
                        "receiver_id": _player_id(team, rng.randrange(1, 12))
                    } if success else {},
                }
            elif draw < 0.88:
                event_type = "carry"
                values = {
                    "end_x": round(min(100.0, x + rng.uniform(2, 20)), 1),
                    "end_y": round(min(100.0, max(0.0, y + rng.uniform(-18, 18))), 1),
                    "outcome": "success" if rng.random() < 0.76 else "fail",
                }
            elif draw < (0.895 if info.match_id == "m03" else 0.91):
                event_type = "shot"
                if info.match_id == "m03" and current_team == "away":
                    xg = round(rng.uniform(0.001, 0.012), 3)
                else:
                    xg = round(rng.uniform(0.01, 0.19), 3)
                values = {
                    "outcome": "success" if rng.random() < 0.34 else "fail",
                    "xg": xg,
                }
            elif draw < 0.95:
                event_type = "tackle"
                values = {"outcome": "success" if rng.random() < 0.72 else "fail"}
            elif draw < 0.985:
                event_type = "interception"
                values = {"outcome": "success" if rng.random() < 0.68 else "fail"}
            else:
                event_type = "foul"
            _add_draft(
                drafts,
                minute=minute,
                second=second,
                team=current_team,
                player_id=player_id,
                event_type=event_type,
                x=x,
                y=y,
                **values,
            )


def generate_match(match_id: str) -> list[Event]:
    raw = _read_yaml("storylines.yaml")["storylines"]
    if match_id not in raw:
        raise ValueError(f"Unknown match id: {match_id}")
    story = raw[match_id]
    info = get_match_info(match_id)
    rng = random.Random(info.seed)
    drafts: list[_Draft] = []

    _add_draft(
        drafts,
        minute=0,
        second=0,
        team="home",
        player_id=None,
        event_type="kickoff",
        detail={"formation": info.home.formation},
    )
    _add_draft(
        drafts,
        minute=0,
        second=0,
        team="away",
        player_id=None,
        event_type="kickoff",
        detail={"formation": info.away.formation},
    )
    _add_draft(
        drafts,
        minute=46,
        second=0,
        team="home",
        player_id=None,
        event_type="kickoff",
        detail={"formation": info.home.formation},
    )
    _inject_beats(drafts, story, info.home, info.away)
    _random_play(drafts, info, rng)
    _add_draft(
        drafts,
        minute=45,
        second=59,
        team=None,
        player_id=None,
        event_type="half_time",
    )
    _add_draft(
        drafts,
        minute=90,
        second=59,
        team=None,
        player_id=None,
        event_type="full_time",
    )
    drafts.sort(key=lambda draft: (draft.clock, draft.order))

    event_ids = {
        draft.key: f"{match_id}-e{seq:04d}"
        for seq, draft in enumerate(drafts, start=1)
        if draft.key is not None
    }
    events: list[Event] = []
    for seq, draft in enumerate(drafts, start=1):
        related_ids = tuple(event_ids[key] for key in draft.related_keys)
        events.append(
            Event.model_validate(
                {
                    **draft.values,
                    "event_id": f"{match_id}-e{seq:04d}",
                    "match_id": match_id,
                    "seq": seq,
                    "related_event_ids": related_ids,
                }
            )
        )
    return events


def write_event_log(events: list[Event], path: Path) -> None:
    if not events:
        raise ValueError("Cannot persist an empty event log")
    match_id = events[0].match_id
    if any(event.match_id != match_id for event in events):
        raise ValueError("An event log must contain one match")
    if any(right.seq <= left.seq for left, right in zip(events, events[1:])):
        raise ValueError("Event sequence must be strictly increasing")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(event.model_dump_json() + "\n" for event in events)
    path.write_text(content, encoding="utf-8")


def read_event_log(path: Path) -> list[Event]:
    events = [
        Event.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not events:
        raise ValueError(f"Event log is empty: {path}")
    if any(right.seq <= left.seq for left, right in zip(events, events[1:])):
        raise ValueError(f"Event sequence is not strictly increasing: {path}")
    if any(event.match_id != events[0].match_id for event in events):
        raise ValueError(f"Event log contains more than one match: {path}")
    return events


def load_or_generate_match(
    match_id: str,
    data_dir: Path | None = None,
) -> tuple[MatchInfo, list[Event]]:
    target_dir = data_dir or Path("data/matches")
    path = target_dir / f"{match_id}.jsonl"
    events = read_event_log(path) if path.exists() else generate_match(match_id)
    if not path.exists():
        write_event_log(events, path)
    return get_match_info(match_id), events
