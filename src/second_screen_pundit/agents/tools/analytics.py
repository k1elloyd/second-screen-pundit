from __future__ import annotations

import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from importlib.resources import files
from typing import Any, TypeVar
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    label: str
    value: float | int | str
    unit: str | None = None
    spoken_value: int | float | None = None
    event_ids: list[str]


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    facts: list[Fact]
    table: list[dict[str, Any]] | None = None
    caveats: list[str] = Field(default_factory=list)
    cannot_tell: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def cap_evidence_events(self) -> ToolResult:
        selected = list(
            dict.fromkeys(event_id for fact in self.facts for event_id in fact.event_ids)
        )[:_MAX_EVIDENCE_EVENT_IDS]
        allowed = set(selected)
        object.__setattr__(
            self,
            "facts",
            [
                fact.model_copy(
                    update={
                        "event_ids": [
                            event_id for event_id in fact.event_ids if event_id in allowed
                        ]
                    }
                )
                for fact in self.facts
            ],
        )
        return self


Params = TypeVar("Params", bound=BaseModel)
_MAX_EVIDENCE_EVENT_IDS = 5


@lru_cache(maxsize=1)
def _thresholds() -> dict[str, float]:
    resource = files("second_screen_pundit.engine").joinpath("analytics.yaml")
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("analytics.yaml must contain a mapping")
    return {key: float(value) for key, value in data.items()}


def big_chance_threshold() -> float:
    return _thresholds()["big_chance_xg"]


def is_big_chance(xg: float | None) -> bool:
    """The single big-chance rule, shared by must-say, key moments and the descriptor."""
    return xg is not None and xg >= big_chance_threshold()


def chance_descriptor(xg: float | None) -> str | None:
    if xg is None:
        return None
    if is_big_chance(xg):
        return "a big chance"
    if xg >= _thresholds()["half_chance_xg"]:
        return "a half-chance"
    return None


def window_chance_descriptor(xg: float) -> str:
    """Team-level wording for an xG surge; contains no number, so it can always be spoken."""
    if xg >= _thresholds()["xg_surge_strong"]:
        return "a string of clear chances"
    return "the better of the chances"


_SPOKEN_WHOLE_UNITS = {"%", "percentage points"}
_SPOKEN_ONE_DECIMAL_UNITS = {"actions/min", "turnovers"}


def spoken_value(value: float | int | str, unit: str | None) -> int | float | None:
    """A rounded value for speech, or None when the exact value is already speakable.

    Percentages are spoken as whole numbers and rates to one decimal place. xG is never
    spoken as a number, so it gets no spoken value.
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or unit == "xG":
        return None
    if unit in _SPOKEN_WHOLE_UNITS:
        rounded: int | float = int(Decimal(str(value)).quantize(Decimal("1"), ROUND_HALF_UP))
    elif unit in _SPOKEN_ONE_DECIMAL_UNITS:
        rounded = float(Decimal(str(value)).quantize(Decimal("0.1"), ROUND_HALF_UP))
    else:
        return None
    return None if float(rounded) == float(value) else rounded


def analytics_threshold(name: str) -> float:
    return _thresholds()[name]


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
        fact_id=f"{tool}:{uuid4().hex}:{slug}",
        label=label,
        value=value,
        unit=unit,
        spoken_value=spoken_value(value, unit),
        event_ids=list(dict.fromkeys(event.event_id for event in events))[
            -_MAX_EVIDENCE_EVENT_IDS:
        ],
    )


def _team_events(events: list[Event], team: Team) -> list[Event]:
    return [event for event in events if event.team == team]


def _team_name(events: list[Event], team: Team) -> str:
    """The canonical full name, which every fact label starts with."""
    if not events:
        return team.title()
    return team_name(get_match_info(events[0].match_id), team).full


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
        # Only the opening kick-off sets a starting shape; later restarts never reset it.
        and (event.type == "formation_change" or (event.type == "kickoff" and event.period == 1))
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
    home_name, away_name = _team_name(visible, "home"), _team_name(visible, "away")
    home_goals = [event for event in goals if event.team == "home"]
    away_goals = [event for event in goals if event.team == "away"]
    home_possession, home_pass_evidence = _possession(relevant, "home")
    home_formation, home_formation_events = _formation_at(relevant, "home", minute)
    away_formation, away_formation_events = _formation_at(relevant, "away", minute)
    score_evidence = goals
    facts = [
        _fact("match_state", f"Match-level score through {minute}'", f"{len(home_goals)}-{len(away_goals)}", None, score_evidence),
        _fact("match_state", f"{home_name} possession through {minute}'", home_possession, "%", home_pass_evidence),
        *[
            _fact(
                "match_state",
                f"{_team_name(visible, team)} shots through {minute}'",
                sum(event.team == team for event in shots),
                "shots",
                [event for event in shots if event.team == team],
            )
            for team in ("home", "away")
        ],
        _fact("match_state", f"Match-level total shots through {minute}'", len(shots), "shots", shots),
        _fact("match_state", f"{home_name} xG through {minute}'", round(sum(event.xg or 0 for event in shots if event.team == "home"), 3), "xG", [event for event in shots if event.team == "home"]),
        _fact("match_state", f"{away_name} xG through {minute}'", round(sum(event.xg or 0 for event in shots if event.team == "away"), 3), "xG", [event for event in shots if event.team == "away"]),
        _fact("match_state", f"{home_name} cards through {minute}'", sum(event.team == "home" for event in cards), "cards", [event for event in cards if event.team == "home"]),
        _fact("match_state", f"{away_name} cards through {minute}'", sum(event.team == "away" for event in cards), "cards", [event for event in cards if event.team == "away"]),
        _fact("match_state", f"{home_name} formation through {minute}'", home_formation, None, home_formation_events),
        _fact("match_state", f"{away_name} formation through {minute}'", away_formation, None, away_formation_events),
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
            f"{_team_name(visible, team)} possession, {start}-{end}'",
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
                    f"{_team_name(visible, team)} cumulative xG through {bucket}'",
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
        _fact("shot_summary", f"{_team_name(visible, params.team)} shots, {start}-{end}'", len(shots), "shots", shots),
        _fact("shot_summary", f"{_team_name(visible, params.team)} shots on target, {start}-{end}'", len(on_target), "shots", on_target),
        _fact("shot_summary", f"{_team_name(visible, params.team)} xG, {start}-{end}'", round(sum(event.xg or 0 for event in shots), 3), "xG", shots),
        _fact(
            "shot_summary",
            f"{_team_name(visible, params.team)} best chance xG, {start}-{end}'",
            best.xg or 0.0 if best else 0.0,
            "xG",
            [best] if best else [],
        ),
    ]
    descriptor = chance_descriptor(best.xg if best is not None else None)
    if best is not None and descriptor is not None:
        facts.append(
            _fact(
                "shot_summary",
                f"{_team_name(visible, params.team)} best chance description, {start}-{end}'",
                descriptor,
                None,
                [best],
            )
        )
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
        _fact("pressing_intensity", f"{_team_name(visible, params.team)} defensive actions per minute, {start}-{end}'", rate, "actions/min", defensive),
        _fact("pressing_intensity", f"{_team_name(visible, params.team)} high turnovers, {start}-{end}'", len(high_turnovers), "turnovers", high_turnovers),
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
    info = get_match_info(visible[0].match_id) if visible else None
    player_names: dict[str, str] = {}
    if info is not None:
        squad = info.home if params.team == "home" else info.away
        player_names = {
            player_id: player["name"]
            for player in squad.players
            for player_id in (
                player["id"],
                f"{params.team}-{player['id'].rsplit('-', maxsplit=1)[-1]}",
            )
        }

    def player_name(player_id: str) -> str:
        try:
            return player_names[player_id]
        except KeyError as exc:
            raise ValueError(f"Unknown synthetic player ID in pass network: {player_id}") from exc

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
        _fact("pass_network", f"{_team_name(visible, params.team)} progressive passes, {start}-{end}'", len(progressive), "passes", progressive),
        *[
            _fact(
                "pass_network",
                f"{_team_name(visible, params.team)} passing pair "
                f"{player_name(passer)} to {player_name(receiver)}, {start}-{end}'",
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
        facts=[_fact("formation_at", f"{_team_name(visible, params.team)} formation at {minute}'", formation, None, evidence)],
        caveats=caveats,
    )


def key_moments(
    events: list[Event],
    params: KeyMomentsParams,
    *,
    as_of: int,
) -> ToolResult:
    visible = _visible(events, as_of)
    ranked: list[tuple[tuple[int, float, int], Event, str]] = []
    for event in visible:
        if event.type == "goal":
            ranked.append(((0, 0, event.seq), event, "Goal"))
        elif event.type == "card" and event.detail.get("colour") == "red":
            ranked.append(((1, 0, event.seq), event, "Red card"))
        elif event.type == "formation_change":
            ranked.append(((2, 0, event.seq), event, "Formation change"))
        elif event.type == "shot" and is_big_chance(event.xg):
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
            f"{_team_name(visible, event.team) if event.team else 'Match-level'} {label.casefold()} at {event.minute + event.added_minute}'",
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
        _fact("compare_windows", f"{_team_name(visible, params.team)} {params.metric} before, {before_start}-{before_end}'", before_value, unit, before_events),
        _fact("compare_windows", f"{_team_name(visible, params.team)} {params.metric} after, {after_start}-{after_end}'", after_value, unit, after_events),
        _fact(
            "compare_windows",
            f"{_team_name(visible, params.team)} {params.metric} change, {before_start}-{before_end}' vs {after_start}-{after_end}'",
            delta,
            unit,
            [*before_events, *after_events],
        ),
    ]
    return ToolResult(tool="compare_windows", facts=facts, caveats=caveats)


FULL_TIME_MINUTE = 90


def goal_context(events: list[Event], goal: Event) -> str:
    """Classify a goal from the score immediately before and after it.

    opener: the first goal of the match. equaliser: it levels the score. go-ahead goal: it
    turns a draw into a lead. goal that extends the lead: the scorer was already ahead.
    consolation: the scorer is still behind afterwards.
    """
    if goal.type != "goal" or goal.team is None:
        raise ValueError("goal_context requires a goal event with a team")
    before = [event for event in events if event.type == "goal" and event.seq < goal.seq]
    if not before:
        return "opener"
    own = sum(event.team == goal.team for event in before) + 1
    other = sum(event.team != goal.team for event in before)
    if own == other:
        return "equaliser"
    if own < other:
        return "consolation"
    return "go-ahead goal" if own - 1 == other else "goal that extends the lead"


def match_context(
    events: list[Event],
    *,
    as_of: int,
    goal: Event | None = None,
) -> ToolResult:
    """Deterministic clock facts, plus the goal context when the run is about a goal."""
    visible = _visible(events, as_of)
    if not visible:
        raise ValueError("match_context requires at least one visible event")
    latest = max(visible, key=lambda event: event.seq)
    clock = latest.minute + latest.added_minute
    facts = [
        _fact("match_context", f"Match-level clock at {clock}'", clock, "minute", [latest]),
        _fact(
            "match_context",
            f"Match-level minutes remaining at {clock}'",
            max(0, FULL_TIME_MINUTE - clock),
            "minutes",
            [latest],
        ),
    ]
    if goal is not None and goal.seq <= as_of:
        facts.append(
            _fact(
                "match_context",
                f"{_team_name(visible, goal.team)} goal context at {clock}'",  # type: ignore[arg-type]
                goal_context(visible, goal),
                None,
                [goal],
            )
        )
    return ToolResult(tool="match_context", facts=facts)


SNAPSHOT_RECENT_MINUTES = 15
SNAPSHOT_MOMENTS = 5
_FULL_SQUAD = 11


def _player(info: MatchInfo, team: str | None, player_id: str | None) -> str | None:
    if team not in {"home", "away"} or player_id is None:
        return None
    squad = info.home if team == "home" else info.away
    return next((p.get("name") for p in squad.players if p.get("id") == player_id), None)


def match_snapshot(
    events: list[Event],
    *,
    as_of: int,
    moments: int = SNAPSHOT_MOMENTS,
    minor: bool = True,
) -> list[ToolResult]:
    """What the viewer has seen so far, as cited facts: the Explainer's and Recap's starting point.

    The first result holds the clock, score, formations and per-team possession (whole match
    and the last 15 minutes), shots, xG, cards and players on the pitch. Then one result per
    key moment (the last ``moments``, in match order), so each keeps its own event ID under
    the five-ID evidence cap. A moment's table row describes it in words for the prompt.
    ``minor=False`` leaves out yellow cards and big chances (the recap's turning points).
    """
    visible = _visible(events, as_of)
    if not visible:
        raise ValueError("match_snapshot requires at least one visible event")
    info = get_match_info(visible[0].match_id)
    clock_result = match_context(events, as_of=as_of)
    latest = max(visible, key=lambda event: event.seq)
    clock = latest.minute + latest.added_minute
    goals = [event for event in visible if event.type == "goal"]
    shots = [event for event in visible if event.type == "shot"]
    cards = [event for event in visible if event.type == "card"]
    names = {team: _team_name(visible, team) for team in ("home", "away")}
    home_goals = sum(event.team == "home" for event in goals)
    facts = [
        *clock_result.facts,
        _fact("match_snapshot", f"Match-level score through {clock}'", f"{home_goals}-{len(goals) - home_goals}", None, goals),
    ]
    for team in ("home", "away"):
        formation, formation_events = _formation_at(visible, team, clock)
        facts.append(
            _fact("match_snapshot", f"{names[team]} formation through {clock}'", formation, None, formation_events)
        )
    recent_start = max(0, clock - SNAPSHOT_RECENT_MINUTES)
    recent = [event for event in visible if _in_window(event, recent_start, clock + 1)]
    for team in ("home", "away"):
        name = names[team]
        team_shots = [event for event in shots if event.team == team]
        on_target = [event for event in team_shots if event.outcome == "success"]
        team_cards = [event for event in cards if event.team == team]
        reds = [event for event in team_cards if event.detail.get("colour") == "red"]
        possession, possession_events = _possession(visible, team)
        recent_possession, recent_events = _possession(recent, team)
        facts.extend([
            _fact("match_snapshot", f"{name} possession through {clock}'", possession, "%", possession_events),
            _fact("match_snapshot", f"{name} possession, {recent_start}-{clock + 1}'", recent_possession, "%", recent_events),
            _fact("match_snapshot", f"{name} shots through {clock}'", len(team_shots), "shots", team_shots),
            _fact("match_snapshot", f"{name} shots on target through {clock}'", len(on_target), "shots", on_target),
            _fact("match_snapshot", f"{name} xG through {clock}'", round(sum(event.xg or 0 for event in team_shots), 3), "xG", team_shots),
            _fact("match_snapshot", f"{name} cards through {clock}'", len(team_cards), "cards", team_cards),
            _fact("match_snapshot", f"{name} players on the pitch through {clock}'", _FULL_SQUAD - len(reds), "players", reds),
        ])
    results = [ToolResult(tool="match_snapshot", facts=facts)]

    goal_shot_ids = {related for goal in goals for related in goal.related_event_ids}
    chosen = [
        event
        for event in visible
        if event.type in {"goal", "formation_change", "substitution"}
        or (event.type == "card" and (minor or event.detail.get("colour") == "red"))
        or (
            minor
            and event.type == "shot"
            and is_big_chance(event.xg)
            and event.event_id not in goal_shot_ids
        )
    ][-moments:] if moments else []
    for event in chosen:
        minute = event.minute + event.added_minute
        name = names[event.team] if event.team in names else "Match-level"
        short = team_name(info, event.team).short if event.team else ""
        player = _player(info, event.team, event.player_id) or "a player"
        moment_facts: list[Fact] = []
        if event.type == "goal":
            context = goal_context(visible, event)
            own_goal = " (own goal)" if event.detail.get("own_goal") else ""
            moment_facts.append(_fact("match_snapshot", f"{name} goal at {minute}'", context, None, [event]))
            shot = next(
                (item for item in visible if item.event_id in event.related_event_ids and item.type == "shot"),
                None,
            )
            chance = chance_descriptor(shot.xg) if shot is not None else None
            # The running score, so a recap can tell a comeback ("Port Calder lead 2-0").
            so_far = [goal for goal in goals if goal.seq <= event.seq]
            home_so_far = sum(goal.team == "home" for goal in so_far)
            away_so_far = len(so_far) - home_so_far
            moment_facts.append(
                _fact("match_snapshot", f"Match-level score after the goal at {minute}'", f"{home_so_far}-{away_so_far}", None, [event])
            )
            if home_so_far == away_so_far:
                running = f"level at {home_so_far}-{away_so_far}"
            elif home_so_far > away_so_far:
                running = f"{team_name(info, 'home').short} lead {home_so_far}-{away_so_far}"
            else:
                running = f"{team_name(info, 'away').short} lead {away_so_far}-{home_so_far}"
            description = (
                f"{player} scores for {short}{own_goal}: the {context}"
                + (f", converting {chance}" if chance else "")
                + f"; {running}"
            )
        elif event.type == "card":
            colour = str(event.detail.get("colour", "yellow"))
            moment_facts.append(_fact("match_snapshot", f"{name} {colour} card at {minute}'", f"{colour} card", None, [event]))
            description = (
                f"{player} of {short} is sent off" if colour == "red" else f"{player} of {short} is booked"
            )
        elif event.type == "formation_change":
            before, _ = _formation_at([item for item in visible if item.seq < event.seq], event.team, minute)  # type: ignore[arg-type]
            after = str(event.detail.get("formation"))
            moment_facts.append(_fact("match_snapshot", f"{name} formation change at {minute}'", after, None, [event]))
            moment_facts.append(_fact("match_snapshot", f"{name} formation before the change at {minute}'", before, None, [event]))
            description = f"{short} change shape from {before} to {after}"
        elif event.type == "substitution":
            player_on = _player(info, event.team, event.detail.get("player_on")) or "a substitute"
            player_off = _player(info, event.team, event.detail.get("player_off")) or "a player"
            moment_facts.append(_fact("match_snapshot", f"{name} substitution at {minute}'", "substitution", None, [event]))
            description = f"{player_on} replaces {player_off} for {short}"
        else:
            on_target = "on target" if event.outcome == "success" else "off target"
            moment_facts.append(_fact("match_snapshot", f"{name} big chance at {minute}'", "big chance", None, [event]))
            description = f"{player} has a big chance for {short}, {on_target}"
        results.append(
            ToolResult(
                tool="match_snapshot",
                facts=moment_facts,
                table=[{"fact_id": moment_facts[0].fact_id, "minute": minute, "description": description}],
            )
        )
    return results


def chance_summary(events: list[Event], *, as_of: int) -> ToolResult:
    """The chances each team had, in words a viewer can hear: never an xG decimal.

    Per team: the count of big chances (the one big-chance rule) and the match xG rounded half
    up to a whole number of expected goals ("about 4 expected goals"). The team with more xG
    also gets the live commentary's wording, "the better of the chances".
    """
    visible = _visible(events, as_of)
    if not visible:
        raise ValueError("chance_summary requires at least one visible event")
    latest = max(visible, key=lambda event: event.seq)
    clock = latest.minute + latest.added_minute
    shots = [event for event in visible if event.type == "shot"]
    xg: dict[Team, float] = {}
    facts: list[Fact] = []
    for team in ("home", "away"):
        name = _team_name(visible, team)
        team_shots = [event for event in shots if event.team == team]
        big = [event for event in team_shots if is_big_chance(event.xg)]
        xg[team] = sum(event.xg or 0 for event in team_shots)
        rounded = int(Decimal(str(round(xg[team], 3))).quantize(Decimal("1"), ROUND_HALF_UP))
        facts.extend([
            _fact("chance_summary", f"{name} big chances through {clock}'", len(big), "big chances", big),
            _fact("chance_summary", f"{name} expected goals, rounded, through {clock}'", rounded, "expected goals", team_shots),
        ])
    if xg["home"] != xg["away"]:
        leader: Team = "home" if xg["home"] > xg["away"] else "away"
        facts.append(
            _fact(
                "chance_summary",
                f"{_team_name(visible, leader)} chance quality through {clock}'",
                "the better of the chances",
                None,
                [event for event in shots if event.team == leader],
            )
        )
    return ToolResult(tool="chance_summary", facts=facts)


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
