"""The viewer profile: how the same verified facts are presented to one viewer.

Mode picks the depth (casual, the default, or analyst); My team picks a side to follow (home,
away, or neutral, the default). The page keeps it per viewer in sessionStorage and sends it
with Start; the replay holds it, and a change mid-match applies from the next line. It holds no
personal data: two fixed choices, recorded on traces as `app.profile.mode` and
`app.profile.team`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProfileMode = Literal["casual", "analyst"]
ProfileTeam = Literal["home", "away", "neutral"]


@dataclass(frozen=True)
class ViewerProfile:
    mode: ProfileMode = "casual"
    team: ProfileTeam = "neutral"

    def span_attributes(self) -> dict[str, str]:
        return {"app.profile.mode": self.mode, "app.profile.team": self.team}

    def as_dict(self) -> dict[str, str]:
        return {"mode": self.mode, "team": self.team}


DEFAULT_PROFILE = ViewerProfile()
# For callers with no viewer (the latency gate, the console, tests): the full detail, as the
# pipeline wrote before profiles existed. A viewer's replay always has its own profile.
NO_VIEWER_PROFILE = ViewerProfile(mode="analyst")
