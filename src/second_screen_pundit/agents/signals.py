"""Deterministic tactical-candidate signals for periodic live windows.

Every number is computed here from the event log, reusing the analytics tools' metric
definitions, and is exposed as a Fact so the evidence check can trace it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from second_screen_pundit.agents.commentator import team_display_name
from second_screen_pundit.agents.tools.analytics import (
    Fact,
    ToolResult,
    analytics_threshold,
    formation_at,
    possession_window,
    pressing_intensity,
    shot_summary,
)
from second_screen_pundit.agents.tools.parameters import (
    FormationAtParams,
    PossessionWindowParams,
    PressingIntensityParams,
    ShotSummaryParams,
    Team,
)
from second_screen_pundit.engine.schema import Event, MatchInfo


SIGNAL_TOOL = "tactical_signals"
SignalKind = Literal[
    "possession_swing",
    "xg_surge",
    "substitution_effect",
    "red_card",
    "formation_change",
    "high_turnover_surge",
]
MANDATORY_KINDS: frozenset[str] = frozenset({"red_card", "formation_change"})
_ONE_SHOT_KINDS: frozenset[str] = MANDATORY_KINDS | {"substitution_effect"}
_MAX_EVIDENCE_EVENTS = 5


@dataclass(frozen=True)
class TacticalSignal:
    key: str
    kind: SignalKind
    team: Team
    clock: int
    summary: str
    facts: tuple[Fact, ...]

    @property
    def mandatory(self) -> bool:
        return self.kind in MANDATORY_KINDS

    @property
    def one_shot(self) -> bool:
        return self.kind in _ONE_SHOT_KINDS

    @property
    def event_ids(self) -> list[str]:
        return list(dict.fromkeys(event_id for fact in self.facts for event_id in fact.event_ids))


def _clock(event: Event) -> int:
    return event.minute + event.added_minute


def _fact(
    key: str,
    name: str,
    label: str,
    value: float | int | str,
    unit: str | None,
    event_ids: list[str],
) -> Fact:
    return Fact(
        fact_id=f"{SIGNAL_TOOL}:{key}_{name}",
        label=label,
        value=value,
        unit=unit,
        event_ids=list(dict.fromkeys(event_ids))[-_MAX_EVIDENCE_EVENTS:],
    )


def signals_tool_result(signals: list[TacticalSignal]) -> ToolResult:
    return ToolResult(
        tool=SIGNAL_TOOL,
        facts=[fact for signal in signals for fact in signal.facts],
    )


def signal_prompt(signal: TacticalSignal) -> dict[str, object]:
    return {
        "kind": signal.kind,
        "team": signal.team,
        "mandatory": signal.mandatory,
        "summary": signal.summary,
        "facts": [
            {
                "fact_id": fact.fact_id,
                "label": fact.label,
                "value": fact.value,
                "unit": fact.unit,
            }
            for fact in signal.facts
        ],
        "event_ids": signal.event_ids,
    }


class SignalTracker:
    """Evaluates signals at window boundaries; one-shot signals fire once per cause."""

    def __init__(self, info: MatchInfo) -> None:
        self.info = info
        self._seen: set[str] = set()

    def evaluate(self, events: list[Event], *, as_of: int) -> list[TacticalSignal]:
        visible = [event for event in events if event.seq <= as_of]
        if not visible:
            return []
        now = min(90, max(_clock(event) for event in visible))
        signals = self._one_shot_signals(events, visible, now, as_of)
        signals.extend(self._possession_swing(events, now, as_of))
        signals.extend(self._xg_surge(events, now, as_of))
        signals.extend(self._high_turnover_surge(visible, now))
        return sorted(signals, key=lambda signal: not signal.mandatory)

    def _name(self, team: Team) -> str:
        return team_display_name(self.info, team)

    def _one_shot_signals(
        self,
        events: list[Event],
        visible: list[Event],
        now: int,
        as_of: int,
    ) -> list[TacticalSignal]:
        found: list[TacticalSignal] = []
        for event in visible:
            if event.team is None:
                continue
            if event.type == "card" and event.detail.get("colour") == "red":
                key = f"red_card_{event.seq}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                reds = sum(
                    1
                    for other in visible
                    if other.type == "card"
                    and other.team == event.team
                    and other.detail.get("colour") == "red"
                )
                found.append(
                    TacticalSignal(
                        key=key,
                        kind="red_card",
                        team=event.team,
                        clock=_clock(event),
                        summary=(
                            f"{self._name(event.team)} have had a player sent off and "
                            f"are down to {11 - reds} players."
                        ),
                        facts=(
                            _fact(
                                key,
                                "players_on_pitch",
                                f"{self._name(event.team)} players on the pitch",
                                11 - reds,
                                "players",
                                [event.event_id],
                            ),
                        ),
                    )
                )
            elif event.type == "formation_change":
                key = f"formation_{event.seq}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                previous = formation_at(
                    events,
                    FormationAtParams(team=event.team, minute=min(90, _clock(event))),
                    as_of=event.seq - 1,
                ).facts[0]
                new_formation = str(event.detail.get("formation", "unknown"))
                found.append(
                    TacticalSignal(
                        key=key,
                        kind="formation_change",
                        team=event.team,
                        clock=_clock(event),
                        summary=(
                            f"{self._name(event.team)} changed formation from "
                            f"{previous.value} to {new_formation}."
                        ),
                        facts=(
                            _fact(
                                key,
                                "new_formation",
                                f"{self._name(event.team)} new formation",
                                new_formation,
                                None,
                                [event.event_id],
                            ),
                            _fact(
                                key,
                                "previous_formation",
                                f"{self._name(event.team)} previous formation",
                                str(previous.value),
                                None,
                                previous.event_ids,
                            ),
                        ),
                    )
                )
            elif event.type == "substitution":
                signal = self._substitution_effect(events, event, now, as_of)
                if signal is not None:
                    found.append(signal)
        return found

    def _substitution_effect(
        self,
        events: list[Event],
        substitution: Event,
        now: int,
        as_of: int,
    ) -> TacticalSignal | None:
        team = substitution.team
        window = int(analytics_threshold("substitution_effect_window_minutes"))
        at = _clock(substitution)
        key = f"substitution_effect_{substitution.seq}"
        if team is None or key in self._seen or at < window or now < at + window:
            return None
        self._seen.add(key)
        before = pressing_intensity(
            events, PressingIntensityParams(team=team, start=at - window, end=at), as_of=as_of
        )
        after = pressing_intensity(
            events, PressingIntensityParams(team=team, start=at, end=at + window), as_of=as_of
        )
        rate_before, rate_after = (result.facts[0] for result in (before, after))
        turnovers_before, turnovers_after = (result.facts[1] for result in (before, after))
        rate_change = float(rate_after.value) - float(rate_before.value)
        turnover_change = int(turnovers_after.value) - int(turnovers_before.value)
        if (
            abs(rate_change) < analytics_threshold("substitution_pressing_change_per_minute")
            and abs(turnover_change) < analytics_threshold("substitution_high_turnover_change")
        ):
            return None
        name = self._name(team)
        return TacticalSignal(
            key=key,
            kind="substitution_effect",
            team=team,
            clock=at,
            summary=(
                f"After {name}'s substitution, pressing went from {rate_before.value} to "
                f"{rate_after.value} defensive actions per minute and high turnovers from "
                f"{turnovers_before.value} to {turnovers_after.value}, comparing {window} "
                f"minutes either side."
            ),
            facts=(
                _fact(key, "pressing_before", f"{name} pressing before the substitution",
                      rate_before.value, "actions/min", rate_before.event_ids),
                _fact(key, "pressing_after", f"{name} pressing after the substitution",
                      rate_after.value, "actions/min", rate_after.event_ids),
                _fact(key, "high_turnovers_before", f"{name} high turnovers before the substitution",
                      turnovers_before.value, "turnovers", turnovers_before.event_ids),
                _fact(key, "high_turnovers_after", f"{name} high turnovers after the substitution",
                      turnovers_after.value, "turnovers", turnovers_after.event_ids),
                _fact(key, "window", "Comparison window either side of the substitution",
                      window, "minutes", [substitution.event_id]),
            ),
        )

    def _possession_swing(
        self,
        events: list[Event],
        now: int,
        as_of: int,
    ) -> list[TacticalSignal]:
        length = int(analytics_threshold("possession_swing_window_minutes"))
        if now < 2 * length:
            return []
        recent = possession_window(
            events,
            PossessionWindowParams(start=now - length, end=now),
            as_of=as_of,
        )
        prior = possession_window(
            events,
            PossessionWindowParams(start=now - 2 * length, end=now - length),
            as_of=as_of,
        )
        signals: list[TacticalSignal] = []
        for index, team in enumerate(("home", "away")):
            recent_fact, prior_fact = recent.facts[index], prior.facts[index]
            change = round(float(recent_fact.value) - float(prior_fact.value), 1)
            if change <= analytics_threshold("possession_swing_pp"):
                continue
            key = f"possession_swing_{team}"
            name = self._name(team)  # type: ignore[arg-type]
            signals.append(
                TacticalSignal(
                    key=key,
                    kind="possession_swing",
                    team=team,  # type: ignore[arg-type]
                    clock=now,
                    summary=(
                        f"{name} possession rose by {change} percentage points, from "
                        f"{prior_fact.value}% to {recent_fact.value}%, comparing the last "
                        f"{length} minutes with the {length} before."
                    ),
                    facts=(
                        _fact(key, "before", f"{name} possession in the previous {length} minutes",
                              prior_fact.value, "%", prior_fact.event_ids),
                        _fact(key, "after", f"{name} possession in the last {length} minutes",
                              recent_fact.value, "%", recent_fact.event_ids),
                        _fact(key, "change", f"{name} possession swing",
                              change, "percentage points", recent_fact.event_ids),
                        _fact(key, "window", "Possession comparison window",
                              length, "minutes", recent_fact.event_ids),
                    ),
                )
            )
        return signals

    def _xg_surge(self, events: list[Event], now: int, as_of: int) -> list[TacticalSignal]:
        length = int(analytics_threshold("xg_surge_window_minutes"))
        if now < length:
            return []
        xg: dict[str, Fact] = {}
        for team in ("home", "away"):
            summary = shot_summary(
                events,
                ShotSummaryParams(team=team, start=now - length, end=now),
                as_of=as_of,
            )
            xg[team] = next(fact for fact in summary.facts if fact.label == "xG")
        signals: list[TacticalSignal] = []
        for team, opponent in (("home", "away"), ("away", "home")):
            own, other = float(xg[team].value), float(xg[opponent].value)
            if own <= analytics_threshold("xg_surge") or own <= other:
                continue
            key = f"xg_surge_{team}"
            name, other_name = self._name(team), self._name(opponent)  # type: ignore[arg-type]
            signals.append(
                TacticalSignal(
                    key=key,
                    kind="xg_surge",
                    team=team,  # type: ignore[arg-type]
                    clock=now,
                    summary=(
                        f"{name} created {xg[team].value} xG in the last {length} minutes "
                        f"against {xg[opponent].value} for {other_name}."
                    ),
                    facts=(
                        _fact(key, "xg", f"{name} xG in the last {length} minutes",
                              xg[team].value, "xG", xg[team].event_ids),
                        _fact(key, "opponent_xg", f"{other_name} xG in the last {length} minutes",
                              xg[opponent].value, "xG", xg[opponent].event_ids),
                        _fact(key, "window", "xG comparison window",
                              length, "minutes", xg[team].event_ids),
                    ),
                )
            )
        return signals

    def _high_turnover_surge(
        self,
        visible: list[Event],
        now: int,
    ) -> list[TacticalSignal]:
        window = int(analytics_threshold("high_turnover_surge_window_minutes"))
        if now < window:
            return []
        threshold_x = analytics_threshold("high_turnover_x")
        turnovers = [
            event
            for event in visible
            if event.type in {"tackle", "interception"}
            and event.outcome == "success"
            and event.x is not None
            and event.x >= threshold_x
        ]
        recent = [
            event for event in turnovers if now - window <= _clock(event) <= now
        ]
        if not recent:
            return []

        signals: list[TacticalSignal] = []
        for team in ("home", "away"):
            team_recent = [event for event in recent if event.team == team]
            team_total = [event for event in turnovers if event.team == team]
            team_average = len(team_total) * window / now
            if not team_recent or len(team_recent) < 2 * team_average:
                continue
            key = f"high_turnover_surge_{team}"
            name = self._name(team)  # type: ignore[arg-type]
            signals.append(
                TacticalSignal(
                    key=key,
                    kind="high_turnover_surge",
                    team=team,  # type: ignore[arg-type]
                    clock=now,
                    summary=(
                        f"{name} won {len(team_recent)} high turnovers in the last {window} "
                        f"minutes, at least twice their match-to-date average of "
                        f"{team_average:.2f} per {window} minutes."
                    ),
                    facts=(
                        _fact(
                            key,
                            "recent",
                            f"{name} high turnovers in the last {window} minutes",
                            len(team_recent),
                            "turnovers",
                            [event.event_id for event in team_recent],
                        ),
                        _fact(
                            key,
                            "match_average",
                            f"{name} match-to-date high turnovers per {window} minutes",
                            round(team_average, 2),
                            "turnovers",
                            [event.event_id for event in team_total],
                        ),
                        _fact(
                            key,
                            "window",
                            "High-turnover comparison window",
                            window,
                            "minutes",
                            [event.event_id for event in team_recent],
                        ),
                    ),
                )
            )
        return signals
