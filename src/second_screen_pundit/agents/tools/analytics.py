from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from importlib.resources import files
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field

from second_screen_pundit.agents.tools.parameters import (
    CompareWindowsParams,
    FormationAtParams,
    KeyMomentsParams,
    MatchStateParams,
    PassNetworkParams,
    PossessionWindowParams,
    PressingIntensityParams,
    ShotSummaryParams,
    Team,
    XgTimelineParams,
)
from second_screen_pundit.engine.generate import get_match_info
from second_screen_pundit.engine.schema import Event


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    label: str
    value: float | int | str
    unit: str | None = None
    event_ids: list[str]


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    facts: list[Fact]
    table: list[dict[str, Any]] | None = None
    caveats: list[str] = Field(default_factory=list)
    cannot_tell: list[str] = Field(default_factory=list)


Params = TypeVar("Params", bound=BaseModel)


@lru_cache(maxsize=1)
def _thresholds() -> dict[str, float]:
    resource = files("second_screen_pundit.engine").joinpath("analytics.yaml")
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("analytics.yaml must contain a mapping")
    return {key: float(value) for key, value in data.items()}


def big_chance_threshold() -> float:
    return _thresholds()["big_chance_xg"]


def _visible(events: list[Event], as_of: int) -> list[Event]:
    if as_of < 0:
        raise ValueError("as_of must be a non-negative event sequence")
    return [event for event in events if event.seq <= as_of]


def _latest_minute(events: list[Event]) -> int:
    return max((event.minute + event.added_minute for event in events), default=0)


def _clamp_minute(
    requested: int,
    events: list[Event],
    caveats: list[str],
    label: str,
) -> int:
    latest = _latest_minute(events)
    if requested > latest:
        caveats.append(f"{label} was clamped from {requested} to {latest} minutes at as_of.")
        return latest
    return requested


def _clamp_window(
    start: int,
    end: int,
    visible: list[Event],
    caveats: list[str],
    label: str,
) -> tuple[int, int]:
    return (
        _clamp_minute(start, visible, caveats, f"{label} start"),
        _clamp_minute(end, visible, caveats, f"{label} end"),
    )


def _in_window(event: Event, start: int, end: int) -> bool:
    clock_minute = event.minute + event.added_minute
    return start <= clock_minute < end


def _fact(
    tool: str,
    label: str,
    value: float | int | str,
    unit: str | None,
    events: list[Event],
) -> Fact:
    slug = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
    return Fact(
        fact_id=f"{tool}:{slug}",
        label=label,
        value=value,
        unit=unit,
        event_ids=list(dict.fromkeys(event.event_id for event in events)),
    )


def _team_events(events: list[Event], team: Team) -> list[Event]:
    return [event for event in events if event.team == team]


def _possession(events: list[Event], team: Team) -> tuple[float, list[Event]]:
    successful = [
        event for event in events if event.type == "pass" and event.outcome == "success"
    ]
    team_passes = [event for event in successful if event.team == team]
    value = (len(team_passes) / len(successful) * 100) if successful else 0.0
    return round(value, 1), successful


def _formation_at(events: list[Event], team: Team, minute: int) -> tuple[str, list[Event]]:
    changes = [
        event
        for event in events
        if event.team == team
        and event.type in {"kickoff", "formation_change"}
        and _in_window(event, 0, minute + 1)
        and isinstance(event.detail.get("formation"), str)
    ]
    if changes:
        latest = max(changes, key=lambda event: event.seq)
        return str(latest.detail["formation"]), [latest]
    info = get_match_info(events[0].match_id) if events else None
    if info is None:
        return "unknown", []
    squad = info.home if team == "home" else info.away
    return squad.formation, []


def match_state(
    events: list[Event],
    params: MatchStateParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    minute = _clamp_minute(params.minute, visible, caveats, "minute")
    relevant = [event for event in visible if event.minute + event.added_minute <= minute]
    goals = [event for event in relevant if event.type == "goal"]
    shots = [event for event in relevant if event.type == "shot"]
    cards = [event for event in relevant if event.type == "card"]
    home_goals = [event for event in goals if event.team == "home"]
    away_goals = [event for event in goals if event.team == "away"]
    home_possession, home_pass_evidence = _possession(relevant, "home")
    home_formation, home_formation_events = _formation_at(relevant, "home", minute)
    away_formation, away_formation_events = _formation_at(relevant, "away", minute)
    score_evidence = goals
    facts = [
        _fact("match_state", "Score", f"{len(home_goals)}-{len(away_goals)}", None, score_evidence),
        _fact("match_state", "Home possession", home_possession, "%", home_pass_evidence),
        _fact("match_state", "Shots", len(shots), "shots", shots),
        _fact("match_state", "Home xG", round(sum(event.xg or 0 for event in shots if event.team == "home"), 3), "xG", [event for event in shots if event.team == "home"]),
        _fact("match_state", "Away xG", round(sum(event.xg or 0 for event in shots if event.team == "away"), 3), "xG", [event for event in shots if event.team == "away"]),
        _fact("match_state", "Home cards", sum(event.team == "home" for event in cards), "cards", [event for event in cards if event.team == "home"]),
        _fact("match_state", "Away cards", sum(event.team == "away" for event in cards), "cards", [event for event in cards if event.team == "away"]),
        _fact("match_state", "Home formation", home_formation, None, home_formation_events),
        _fact("match_state", "Away formation", away_formation, None, away_formation_events),
    ]
    return ToolResult(tool="match_state", facts=facts, caveats=caveats)


def possession_window(
    events: list[Event],
    params: PossessionWindowParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    start, end = _clamp_window(params.start, params.end, visible, caveats, "window")
    window = [event for event in visible if _in_window(event, start, end)]
    successful = [
        event for event in window if event.type == "pass" and event.outcome == "success"
    ]
    denominator = len(successful)
    facts = [
        _fact(
            "possession_window",
            f"{team.title()} possession",
            round(sum(event.team == team for event in successful) / denominator * 100, 1)
            if denominator
            else 0.0,
            "%",
            successful,
        )
        for team in ("home", "away")
    ]
    return ToolResult(tool="possession_window", facts=facts, caveats=caveats)


def xg_timeline(
    events: list[Event],
    params: XgTimelineParams,
    *,
    as_of: int,
) -> ToolResult:
    del params
    visible = _visible(events, as_of)
    buckets: dict[int, dict[str, list[Event]]] = {}
    for event in visible:
        if event.type == "shot":
            bucket = max(0, ((event.minute - 1) // 5 + 1) * 5)
            buckets.setdefault(bucket, {"home": [], "away": []})[event.team or "home"].append(event)
    cumulative = {"home": 0.0, "away": 0.0}
    facts: list[Fact] = []
    rows: list[dict[str, Any]] = []
    for bucket in range(5, _latest_minute(visible) + 1, 5):
        bucket_events = buckets.get(bucket, {"home": [], "away": []})
        row: dict[str, Any] = {"through_minute": bucket}
        for team in ("home", "away"):
            shots = bucket_events[team]
            cumulative[team] += sum(event.xg or 0 for event in shots)
            value = round(cumulative[team], 3)
            row[f"{team}_xg"] = value
            facts.append(
                _fact(
                    "xg_timeline",
                    f"{team.title()} cumulative xG through {bucket}'",
                    value,
                    "xG",
                    [event for prior_bucket in range(5, bucket + 1, 5) for event in buckets.get(prior_bucket, {"home": [], "away": []})[team]],
                )
            )
        rows.append(row)
    return ToolResult(tool="xg_timeline", facts=facts, table=rows)


def shot_summary(
    events: list[Event],
    params: ShotSummaryParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    start, end = _clamp_window(params.start, params.end, visible, caveats, "window")
    shots = [
        event
        for event in visible
        if event.team == params.team and event.type == "shot" and _in_window(event, start, end)
    ]
    on_target = [event for event in shots if event.outcome == "success"]
    best = max(shots, key=lambda event: (event.xg or 0, -event.seq), default=None)
    facts = [
        _fact("shot_summary", "Shots", len(shots), "shots", shots),
        _fact("shot_summary", "On target", len(on_target), "shots", on_target),
        _fact("shot_summary", "xG", round(sum(event.xg or 0 for event in shots), 3), "xG", shots),
        _fact(
            "shot_summary",
            "Best chance",
            best.xg or 0.0 if best else 0.0,
            "xG",
            [best] if best else [],
        ),
    ]
    return ToolResult(tool="shot_summary", facts=facts, caveats=caveats)


def pressing_intensity(
    events: list[Event],
    params: PressingIntensityParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    start, end = _clamp_window(params.start, params.end, visible, caveats, "window")
    window = [
        event
        for event in visible
        if event.team == params.team and _in_window(event, start, end)
    ]
    thresholds = _thresholds()
    defensive = [
        event
        for event in window
        if event.type in {"tackle", "interception", "foul"}
        and event.x is not None
        and event.x >= thresholds["pressing_x"]
    ]
    high_turnovers = [
        event
        for event in window
        if event.type in {"tackle", "interception"}
        and event.outcome == "success"
        and event.x is not None
        and event.x >= thresholds["high_turnover_x"]
    ]
    duration = max(0, end - start)
    rate = round(len(defensive) / duration, 2) if duration else 0.0
    facts = [
        _fact("pressing_intensity", "Defensive actions per minute", rate, "actions/min", defensive),
        _fact("pressing_intensity", "High turnovers", len(high_turnovers), "turnovers", high_turnovers),
    ]
    return ToolResult(tool="pressing_intensity", facts=facts, caveats=caveats)


def pass_network(
    events: list[Event],
    params: PassNetworkParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    start, end = _clamp_window(params.start, params.end, visible, caveats, "window")
    passes = [
        event
        for event in visible
        if event.team == params.team
        and event.type == "pass"
        and event.outcome == "success"
        and _in_window(event, start, end)
    ]
    pairs: Counter[tuple[str, str]] = Counter()
    pair_events: dict[tuple[str, str], list[Event]] = {}
    for event in passes:
        receiver_id = event.detail.get("receiver_id")
        if event.player_id is not None and isinstance(receiver_id, str):
            pair = (event.player_id, receiver_id)
            pairs[pair] += 1
            pair_events.setdefault(pair, []).append(event)
    top_pairs = sorted(pairs, key=lambda pair: (-pairs[pair], pair))[:10]
    thresholds = _thresholds()
    progressive = [
        event
        for event in passes
        if event.x is not None
        and event.end_x is not None
        and event.end_x - event.x >= thresholds["progressive_pass_delta_x"]
    ]
    rows = [
        {"passer_id": passer, "receiver_id": receiver, "passes": pairs[(passer, receiver)]}
        for passer, receiver in top_pairs
    ]
    facts = [
        _fact("pass_network", "Progressive passes", len(progressive), "passes", progressive),
        *[
            _fact(
                "pass_network",
                f"Passing pair {passer} to {receiver}",
                pairs[(passer, receiver)],
                "passes",
                pair_events[(passer, receiver)],
            )
            for passer, receiver in top_pairs
        ],
    ]
    return ToolResult(tool="pass_network", facts=facts, table=rows, caveats=caveats)


def formation_at(
    events: list[Event],
    params: FormationAtParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    minute = _clamp_minute(params.minute, visible, caveats, "minute")
    formation, evidence = _formation_at(visible, params.team, minute)
    return ToolResult(
        tool="formation_at",
        facts=[_fact("formation_at", f"{params.team.title()} formation", formation, None, evidence)],
        caveats=caveats,
    )


def key_moments(
    events: list[Event],
    params: KeyMomentsParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    thresholds = _thresholds()
    ranked: list[tuple[tuple[int, float, int], Event, str]] = []
    for event in visible:
        if event.type == "goal":
            ranked.append(((0, 0, event.seq), event, "Goal"))
        elif event.type == "card" and event.detail.get("colour") == "red":
            ranked.append(((1, 0, event.seq), event, "Red card"))
        elif event.type == "formation_change":
            ranked.append(((2, 0, event.seq), event, "Formation change"))
        elif event.type == "shot" and (event.xg or 0) > thresholds["big_chance_xg"]:
            ranked.append(((3, -(event.xg or 0), event.seq), event, "Big chance"))
        elif event.type == "card" and event.detail.get("colour") == "yellow":
            ranked.append(((4, 0, event.seq), event, "Yellow card"))
    ranked.sort(key=lambda entry: entry[0])
    chosen = ranked[: params.limit]
    rows = [
        {
            "minute": event.minute + event.added_minute,
            "label": label,
            "team": event.team,
            "event_id": event.event_id,
        }
        for _, event, label in chosen
    ]
    facts = [
        _fact(
            "key_moments",
            f"{label} at {event.minute + event.added_minute}'",
            event.type,
            None,
            [event],
        )
        for _, event, label in chosen
    ]
    return ToolResult(tool="key_moments", facts=facts, table=rows)


def _metric_value(
    metric: str,
    team: Team,
    events: list[Event],
    start: int,
    end: int,
) -> tuple[float | int, str, list[Event]]:
    window = [event for event in events if _in_window(event, start, end)]
    team_events = _team_events(window, team)
    if metric == "possession":
        value, evidence = _possession(window, team)
        return value, "%", evidence
    if metric == "xg":
        shots = [event for event in team_events if event.type == "shot"]
        return round(sum(event.xg or 0 for event in shots), 3), "xG", shots
    if metric == "shots":
        shots = [event for event in team_events if event.type == "shot"]
        return len(shots), "shots", shots
    if metric == "pressing_intensity":
        threshold = _thresholds()["pressing_x"]
        actions = [
            event
            for event in team_events
            if event.type in {"tackle", "interception", "foul"}
            and event.x is not None
            and event.x >= threshold
        ]
        duration = max(0, end - start)
        return (round(len(actions) / duration, 2) if duration else 0.0), "actions/min", actions
    if metric == "high_turnovers":
        threshold = _thresholds()["high_turnover_x"]
        turnovers = [
            event
            for event in team_events
            if event.type in {"tackle", "interception"}
            and event.outcome == "success"
            and event.x is not None
            and event.x >= threshold
        ]
        return len(turnovers), "turnovers", turnovers
    threshold = _thresholds()["progressive_pass_delta_x"]
    progressive = [
        event
        for event in team_events
        if event.type == "pass"
        and event.outcome == "success"
        and event.x is not None
        and event.end_x is not None
        and event.end_x - event.x >= threshold
    ]
    return len(progressive), "passes", progressive


def compare_windows(
    events: list[Event],
    params: CompareWindowsParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    caveats: list[str] = []
    before_start, before_end = _clamp_window(
        params.before.start, params.before.end, visible, caveats, "before"
    )
    after_start, after_end = _clamp_window(
        params.after.start, params.after.end, visible, caveats, "after"
    )
    before_value, unit, before_events = _metric_value(
        params.metric, params.team, visible, before_start, before_end
    )
    after_value, _, after_events = _metric_value(
        params.metric, params.team, visible, after_start, after_end
    )
    delta = round(after_value - before_value, 3)
    facts = [
        _fact("compare_windows", "Before", before_value, unit, before_events),
        _fact("compare_windows", "After", after_value, unit, after_events),
        _fact(
            "compare_windows",
            "Change",
            delta,
            unit,
            [*before_events, *after_events],
        ),
    ]
    return ToolResult(tool="compare_windows", facts=facts, caveats=caveats)


TOOL_PARAMETERS: dict[str, type[BaseModel]] = {
    "match_state": MatchStateParams,
    "possession_window": PossessionWindowParams,
    "xg_timeline": XgTimelineParams,
    "shot_summary": ShotSummaryParams,
    "pressing_intensity": PressingIntensityParams,
    "pass_network": PassNetworkParams,
    "formation_at": FormationAtParams,
    "key_moments": KeyMomentsParams,
    "compare_windows": CompareWindowsParams,
}

TOOL_FUNCTIONS = {
    "match_state": match_state,
    "possession_window": possession_window,
    "xg_timeline": xg_timeline,
    "shot_summary": shot_summary,
    "pressing_intensity": pressing_intensity,
    "pass_network": pass_network,
    "formation_at": formation_at,
    "key_moments": key_moments,
    "compare_windows": compare_windows,
}


def invoke_tool(
    events: list[Event],
    tool_name: str,
    parameters: dict[str, Any],
    *,
    as_of: int,
) -> ToolResult:
    if tool_name not in TOOL_PARAMETERS:
        raise ValueError(f"Unknown analytics tool: {tool_name}")
    parsed = TOOL_PARAMETERS[tool_name].model_validate(parameters)
    return TOOL_FUNCTIONS[tool_name](events, parsed, as_of=as_of)
