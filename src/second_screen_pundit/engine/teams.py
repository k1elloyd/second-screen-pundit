"""Canonical team names: one full name per team, plus one accepted short form.

Fact and signal labels always start with the full name. Spoken text may use either form,
so the evidence check accepts both when it looks for a team in a sentence or a label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from second_screen_pundit.engine.schema import MatchInfo


Side = Literal["home", "away"]


@dataclass(frozen=True)
class TeamName:
    full: str
    short: str

    @property
    def forms(self) -> tuple[str, ...]:
        """Accepted spellings, longest first so a full-name prefix wins over its short form."""
        return tuple(dict.fromkeys(sorted((self.full, self.short), key=len, reverse=True)))


def short_name(full: str) -> str:
    return re.sub(r"\s+FC$", "", full, flags=re.IGNORECASE)


def team_name(info: MatchInfo, side: str) -> TeamName:
    full = info.home.name if side == "home" else info.away.name
    return TeamName(full=full, short=short_name(full))


def opponent(side: Side) -> Side:
    return "away" if side == "home" else "home"


def name_mentioned(team: TeamName, text: str) -> bool:
    folded = text.casefold()
    return any(form.casefold() in folded for form in team.forms)


def label_side(label: str, info: MatchInfo) -> Side | None:
    """The team whose name starts the label, accepting either the full or the short form."""
    folded = label.casefold()
    candidates = [
        (len(form), side)
        for side in ("home", "away")
        for form in team_name(info, side).forms
        if folded.startswith(form.casefold())
    ]
    return max(candidates)[1] if candidates else None  # type: ignore[return-value]
