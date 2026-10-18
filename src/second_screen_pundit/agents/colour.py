from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from second_screen_pundit.agents.commentator import live_line_id
from second_screen_pundit.agents.delivery import TIER_TONES
from second_screen_pundit.agents.models import CommentaryLine, Insight
from second_screen_pundit.agents.signals import (
    RATE_DESCRIPTION,
    SignalTracker,
    signals_tool_result,
)
from second_screen_pundit.agents.tools.analytics import (
    Fact,
    ToolResult,
    formation_at,
    possession_window,
    shot_summary,
)
from second_screen_pundit.agents.tools.parameters import (
    FormationAtParams,
    PossessionWindowParams,
    ShotSummaryParams,
)
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import team_name


COLOUR_IDLE_REAL_SECONDS = 12
COLOUR_SPACING_REAL_SECONDS = 40
COLOUR_SPACING_MATCH_MINUTES = 15
COLOUR_FIRST_MINUTE = 10
COLOUR_LAST_MINUTE = 85
ColourStatistic = Literal["possession", "shots_on_target", "turnovers", "formation_matchup"]
_CATEGORY_ORDER: tuple[ColourStatistic, ...] = (
    "possession",
    "shots_on_target",
    "turnovers",
    "formation_matchup",
)


@dataclass
class ColourLineGate:
    viewer_state_received: bool = False
    viewer_line_busy: bool = False
    silence_started_at: float | None = None
    last_colour_at: float | None = None
    colour_match_minutes: list[float] = field(default_factory=list)
    used_versions: set[str] = field(default_factory=set)
    used_statistics: set[str] = field(default_factory=set)
    count: int = 0
    generation: int = 0

    def update_viewer_queue(self, busy: bool, now: float) -> None:
        changed = not self.viewer_state_received or self.viewer_line_busy != busy
        self.viewer_state_received = True
        self.viewer_line_busy = busy
        if busy:
            self.silence_started_at = None
        elif changed or self.silence_started_at is None:
            self.silence_started_at = now
        if changed:
            self.generation += 1

    def record_event(self) -> None:
        self.generation += 1

    def record_real_line(self, now: float, *, audio_enabled: bool) -> None:
        self.generation += 1
        if audio_enabled:
            self.viewer_line_busy = True
            self.silence_started_at = None
        elif not self.viewer_line_busy:
            self.silence_started_at = now

    def is_due(
        self,
        now: float,
        match_minute: float,
        *,
        event_pending: bool,
        stream_open: bool,
        replay_running: bool,
    ) -> bool:
        if (
            not self.viewer_state_received
            or self.viewer_line_busy
            or self.silence_started_at is None
            or event_pending
            or not stream_open
            or not replay_running
            or match_minute < COLOUR_FIRST_MINUTE
            or match_minute > COLOUR_LAST_MINUTE
        ):
            return False
        if now - self.silence_started_at < COLOUR_IDLE_REAL_SECONDS:
            return False
        if (
            self.last_colour_at is not None
            and now - self.last_colour_at < COLOUR_SPACING_REAL_SECONDS
        ):
            return False
        return all(
            match_minute - previous >= COLOUR_SPACING_MATCH_MINUTES
            for previous in self.colour_match_minutes
        )

    def candidate_is_current(self, generation: int) -> bool:
        return self.generation == generation

    def record_colour_release(
        self,
        version: str,
        statistic_keys: tuple[str, ...],
        match_minute: float,
        now: float,
        *,
        audio_enabled: bool,
    ) -> None:
        self.used_versions.add(version)
        self.used_statistics.update(statistic_keys)
        self.count += 1
        self.last_colour_at = now
        self.colour_match_minutes = [
            minute
            for minute in self.colour_match_minutes
            if match_minute - minute < COLOUR_SPACING_MATCH_MINUTES
        ]
        self.colour_match_minutes.append(match_minute)
        self.record_real_line(now, audio_enabled=audio_enabled)


@dataclass(frozen=True)
class ColourLineCandidate:
    statistic: ColourStatistic
    version: str
    statistic_keys: tuple[str, ...]
    lane: str
    line: CommentaryLine
    insight: Insight
    tool_results: tuple[ToolResult, ...]


def _number(fact: Fact) -> str:
    value = fact.spoken_value if fact.spoken_value is not None else fact.value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _candidate(
    statistic: ColourStatistic,
    version: str,
    statistic_keys: tuple[str, ...],
    text: str,
    facts: list[Fact],
    tool_results: list[ToolResult],
    *,
    info: MatchInfo,
    as_of: int,
    clock: int,
    ordinal: int,
) -> ColourLineCandidate | None:
    fact_ids = list(dict.fromkeys(fact.fact_id for fact in facts))
    supplied_event_ids = {
        event_id
        for result in tool_results
        for fact in result.facts
        for event_id in fact.event_ids
    }
    event_ids = list(
        dict.fromkeys(
            event_id
            for fact in facts
            for event_id in fact.event_ids
            if event_id in supplied_event_ids
        )
    )
    if not fact_ids or not event_ids:
        return None
    lane = f"colour_{ordinal}"
    insight = Insight(
        insight_id=f"{info.match_id}-colour-i{ordinal:03d}",
        minute=clock,
        headline=f"Colour line: {statistic.replace('_', ' ')}",
        importance=0.0,
        fact_ids=fact_ids,
        event_ids=event_ids,
    )
    line = CommentaryLine(
        line_id=live_line_id(info.match_id, as_of, lane),
        text=text,
        insight_id=insight.insight_id,
        fact_ids=fact_ids,
        event_ids=event_ids,
        tone=TIER_TONES["routine"],
        source="colour",
        delivery_tier="routine",
    )
    return ColourLineCandidate(
        statistic,
        version,
        statistic_keys,
        lane,
        line,
        insight,
        tuple(tool_results),
    )


def _visible_clock(events: list[Event], as_of: int) -> int:
    return min(
        90,
        max(event.minute + event.added_minute for event in events if event.seq <= as_of),
    )


def _as_of_minute(events: list[Event], as_of: int, minute: int) -> int:
    return max(
        (
            event.seq
            for event in events
            if event.seq <= as_of and event.minute + event.added_minute <= minute
        ),
        default=0,
    )


def _possession_candidates(
    info: MatchInfo,
    events: list[Event],
    *,
    as_of: int,
    clock: int,
    ordinal: int,
    used_versions: set[str],
    used_statistics: set[str],
) -> list[ColourLineCandidate]:
    home_name = team_name(info, "home").full
    away_name = team_name(info, "away").full
    specs: list[tuple[str, str, int, int, str]] = []
    if clock >= COLOUR_FIRST_MINUTE:
        specs.append(("possession-match-v1", "match_to_date", 0, min(91, clock + 1), "match"))
    if clock >= 45:
        first_half_as_of = _as_of_minute(events, as_of, 45)
        if first_half_as_of:
            specs.append(("possession-first-half-v1", "first_half", 0, 45, "first"))
        if clock > 45:
            specs.append(
                ("possession-second-half-v1", "second_half", 45, min(91, clock + 1), "second")
            )
    candidates: list[ColourLineCandidate] = []
    for version, stat_key, start, end, phase in specs:
        key = f"possession:{stat_key}"
        if version in used_versions or key in used_statistics:
            continue
        source_as_of = _as_of_minute(events, as_of, 45) if phase == "first" else as_of
        result = possession_window(
            events,
            PossessionWindowParams(start=start, end=end),
            as_of=source_as_of,
        )
        home, away = result.facts
        if not home.event_ids or not away.event_ids:
            continue
        if phase == "first":
            text = (
                f"In the first half, {home_name} had {_number(home)}% of the possession, "
                f"with {away_name} on {_number(away)}%."
            )
        elif phase == "second":
            text = (
                f"Since the break, {home_name} have had {_number(home)}% of the possession, "
                f"with {away_name} on {_number(away)}%."
            )
        else:
            text = (
                f"{home_name} have had {_number(home)}% of the possession, "
                f"with {away_name} on {_number(away)}%."
            )
        candidate = _candidate(
            "possession",
            version,
            (key,),
            text,
            [home, away],
            [result],
            info=info,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _shots_candidates(
    info: MatchInfo,
    events: list[Event],
    *,
    as_of: int,
    clock: int,
    ordinal: int,
    used_versions: set[str],
    used_statistics: set[str],
) -> list[ColourLineCandidate]:
    home_name = team_name(info, "home").full
    away_name = team_name(info, "away").full
    specs: list[tuple[str, str, int, int, str]] = []
    if clock >= COLOUR_FIRST_MINUTE:
        specs.append(("shots-match-v1", "match_to_date", 0, min(91, clock + 1), "match"))
    if clock >= 45:
        first_half_as_of = _as_of_minute(events, as_of, 45)
        if first_half_as_of:
            specs.append(("shots-first-half-v1", "first_half", 0, 45, "first"))
        if clock > 45:
            specs.append(
                ("shots-second-half-v1", "second_half", 45, min(91, clock + 1), "second")
            )

    candidates: list[ColourLineCandidate] = []
    for version, stat_key, start, end, phase in specs:
        key = f"shots_on_target:{stat_key}"
        if version in used_versions or key in used_statistics:
            continue
        source_as_of = _as_of_minute(events, as_of, 45) if phase == "first" else as_of
        results = [
            shot_summary(
                events,
                ShotSummaryParams(team=side, start=start, end=end),
                as_of=source_as_of,
            )
            for side in ("home", "away")
        ]
        facts = [
            next(fact for fact in result.facts if "shots on target" in fact.label.casefold())
            for result in results
        ]
        shot_events = [
            event_id
            for result in results
            for fact in result.facts
            if " shots," in fact.label.casefold()
            for event_id in fact.event_ids
        ]
        if not shot_events:
            continue
        facts = [
            fact.model_copy(update={"event_ids": list(dict.fromkeys(shot_events))})
            if not fact.event_ids
            else fact
            for fact in facts
        ]
        evidence_results = [
            ToolResult(tool=result.tool, facts=[fact])
            for result, fact in zip(results, facts, strict=True)
        ]
        if phase == "first":
            text = (
                f"In the first half, {home_name} registered {_number(facts[0])} shots on target; "
                f"{away_name} had {_number(facts[1])}."
            )
        elif phase == "second":
            text = (
                f"Since the break, {home_name} have {_number(facts[0])} shots on target, "
                f"with {away_name} on {_number(facts[1])}."
            )
        else:
            text = (
                f"{home_name} have {_number(facts[0])} shots on target, "
                f"compared with {away_name} on {_number(facts[1])}."
            )
        candidate = _candidate(
            "shots_on_target",
            version,
            (key,),
            text,
            facts,
            evidence_results,
            info=info,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _turnover_candidates(
    info: MatchInfo,
    events: list[Event],
    *,
    as_of: int,
    clock: int,
    ordinal: int,
    used_versions: set[str],
    used_statistics: set[str],
) -> list[ColourLineCandidate]:
    versions = (
        (
            "turnovers-usual-rate-v1",
            "{team} are winning the ball high up the pitch, {rate}.",
        ),
        (
            "turnovers-usual-rate-v2",
            "The press from {team} is producing high turnovers {rate}.",
        ),
        (
            "turnovers-usual-rate-v3",
            "High up the pitch, {team} are winning the ball {rate}.",
        ),
    )
    signals = [
        signal
        for signal in SignalTracker(info).evaluate(events, as_of=as_of)
        if signal.kind == "high_turnover_surge"
    ]
    candidates: list[ColourLineCandidate] = []
    for signal in sorted(signals, key=lambda item: item.team):
        description = next(
            (
                fact
                for fact in signal.facts
                if fact.fact_id.endswith(f"_{RATE_DESCRIPTION}")
                and isinstance(fact.value, str)
                and "usual rate" in fact.value
            ),
            None,
        )
        if description is None:
            continue
        statistic_key = f"turnovers:{signal.team}:{','.join(signal.event_ids)}"
        if statistic_key in used_statistics:
            continue
        for version, template in versions:
            if version in used_versions:
                continue
            candidate = _candidate(
                "turnovers",
                version,
                (statistic_key,),
                template.format(team=team_name(info, signal.team).full, rate=description.value),
                list(signal.facts),
                [signals_tool_result([signal])],
                info=info,
                as_of=as_of,
                clock=clock,
                ordinal=ordinal,
            )
            if candidate is not None:
                candidates.append(candidate)
                break
    return candidates


def _formation_candidates(
    info: MatchInfo,
    events: list[Event],
    *,
    as_of: int,
    clock: int,
    ordinal: int,
    used_versions: set[str],
    used_statistics: set[str],
) -> list[ColourLineCandidate]:
    templates = (
        (
            "formation-matchup-v1",
            "{home} are set in a {home_shape}, while {away} use a {away_shape}.",
        ),
        (
            "formation-matchup-v2",
            "The current shape has {home} in a {home_shape}, with {away} in a {away_shape}.",
        ),
        (
            "formation-matchup-v3",
            "It's a {home_shape} for {home}, facing a {away_shape} from {away}.",
        ),
    )
    results = [
        formation_at(events, FormationAtParams(team=side, minute=clock), as_of=as_of)
        for side in ("home", "away")
    ]
    facts = [result.facts[0] for result in results]
    if not all(fact.event_ids for fact in facts):
        return []
    key = f"formation_matchup:{facts[0].value}:{facts[1].value}"
    if key in used_statistics:
        return []
    home_name = team_name(info, "home").full
    away_name = team_name(info, "away").full
    for version, template in templates:
        if version in used_versions:
            continue
        candidate = _candidate(
            "formation_matchup",
            version,
            (key,),
            template.format(
                home=home_name,
                away=away_name,
                home_shape=facts[0].value,
                away_shape=facts[1].value,
            ),
            facts,
            results,
            info=info,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
        )
        if candidate is not None:
            return [candidate]
    return []


def build_colour_candidate(
    info: MatchInfo,
    events: list[Event],
    *,
    as_of: int,
    ordinal: int,
    used_versions: set[str],
    used_statistics: set[str],
) -> ColourLineCandidate | None:
    visible = [event for event in events if event.seq <= as_of]
    if not visible:
        return None
    clock = _visible_clock(visible, as_of)
    categories: dict[ColourStatistic, list[ColourLineCandidate]] = {
        "possession": _possession_candidates(
            info,
            events,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
            used_versions=used_versions,
            used_statistics=used_statistics,
        ),
        "shots_on_target": _shots_candidates(
            info,
            events,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
            used_versions=used_versions,
            used_statistics=used_statistics,
        ),
        "turnovers": _turnover_candidates(
            info,
            events,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
            used_versions=used_versions,
            used_statistics=used_statistics,
        ),
        "formation_matchup": _formation_candidates(
            info,
            events,
            as_of=as_of,
            clock=clock,
            ordinal=ordinal,
            used_versions=used_versions,
            used_statistics=used_statistics,
        ),
    }
    offset = (ordinal - 1) % len(_CATEGORY_ORDER)
    order = _CATEGORY_ORDER[offset:] + _CATEGORY_ORDER[:offset]
    return next(
        (candidate for category in order for candidate in categories[category]),
        None,
    )
