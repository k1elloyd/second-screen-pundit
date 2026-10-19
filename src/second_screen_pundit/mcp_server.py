"""Read-only MCP server over the deterministic analytics, run locally over stdio.

    python -m second_screen_pundit.mcp_server

The tools wrap the same analytics functions the agents call, so their results carry the same
facts and fact IDs the evidence check uses. Each call is cut off at the requested minute:
it is answered at ``as_of``, the last event at or before that minute, and the analytics
functions never read past ``as_of``. It imports only the match engine and the analytics; no
model, speech or web app code. It is not mounted in the web app; see DECISIONS.md.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from second_screen_pundit.agents.tools.analytics import (
    ToolResult,
    formation_at,
    key_moments,
    match_state,
    possession_window,
)
from second_screen_pundit.agents.tools.parameters import (
    FormationAtParams,
    KeyMomentsParams,
    MatchStateParams,
    PossessionWindowParams,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event
from second_screen_pundit.engine.teams import short_name

MATCH_IDS = ("m01", "m02", "m03")

Minute = Annotated[int, Field(ge=0, le=90, description="Match minute, 0 to 90.")]
MatchId = Annotated[str, Field(description="A match ID from list_matches, such as m01.")]

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@lru_cache(maxsize=len(MATCH_IDS))
def _events(match_id: str) -> tuple[Event, ...]:
    return tuple(generate_match(match_id))


def _match_events(match_id: str) -> list[Event]:
    if match_id not in MATCH_IDS:
        raise ToolError(f"Unknown match {match_id!r}. Use one of: {', '.join(MATCH_IDS)}.")
    return list(_events(match_id))


def _as_of(events: list[Event], minute: int) -> int:
    """The last event at or before ``minute`` on the match clock.

    Event sequence numbers run in match order, so every event up to this one is at or
    before the minute too.
    """
    if not 0 <= minute <= 90:
        raise ToolError(f"Minute {minute} is outside the match. Use 0 to 90.")
    at_or_before = [event.seq for event in events if event.minute + event.added_minute <= minute]
    if not at_or_before:
        raise ToolError(f"No match events at or before minute {minute}.")
    return max(at_or_before)


def _payload(match_id: str, minute: int, as_of: int, *results: ToolResult) -> dict[str, Any]:
    return {
        "match_id": match_id,
        "minute": minute,
        "as_of": as_of,
        "results": [result.model_dump() for result in results],
    }


def build_server() -> FastMCP:
    """The five read-only tools."""
    # FastMCP's constructor calls logging.basicConfig, which gives a root logger with no
    # handlers a console handler at INFO. Undo it, so building one in tests changes
    # nothing. Run as a script, the SDK logs to stderr; stdout carries the protocol.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        server = FastMCP(
            "second-screen-pundit",
            instructions=(
                "Read-only analytics for three fictional football matches. Every fact has an "
                "ID; nothing after the requested minute is returned."
            ),
        )
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)

    @server.tool(annotations=_READ_ONLY)
    def list_matches() -> dict[str, Any]:
        """List the fictional matches: ID, teams and stadium."""
        matches = []
        for match_id in MATCH_IDS:
            info = get_match_info(match_id)
            matches.append(
                {
                    "id": match_id,
                    "label": f"{info.home.name} v {info.away.name}",
                    "home": info.home.name,
                    "away": info.away.name,
                    "home_short": short_name(info.home.name),
                    "away_short": short_name(info.away.name),
                    "stadium": info.stadium,
                }
            )
        return {"matches": matches}

    @server.tool(name="match_state", annotations=_READ_ONLY)
    def match_state_tool(match_id: MatchId, minute: Minute) -> dict[str, Any]:
        """Score, possession, shots, xG, cards and formations through a minute."""
        events = _match_events(match_id)
        as_of = _as_of(events, minute)
        result = match_state(events, MatchStateParams(minute=minute), as_of=as_of)
        return _payload(match_id, minute, as_of, result)

    @server.tool(name="key_moments", annotations=_READ_ONLY)
    def key_moments_tool(
        match_id: MatchId,
        up_to_minute: Minute,
        limit: Annotated[int, Field(ge=1, le=25, description="At most this many.")] = 10,
    ) -> dict[str, Any]:
        """Goals, red cards, formation changes, big chances and bookings up to a minute."""
        events = _match_events(match_id)
        as_of = _as_of(events, up_to_minute)
        result = key_moments(events, KeyMomentsParams(limit=limit), as_of=as_of)
        return _payload(match_id, up_to_minute, as_of, result)

    @server.tool(name="possession_window", annotations=_READ_ONLY)
    def possession_window_tool(
        match_id: MatchId,
        minute: Minute,
        window: Annotated[
            int, Field(ge=1, le=90, description="Length in minutes, ending at the minute.")
        ] = 10,
    ) -> dict[str, Any]:
        """Each team's possession share over the last `window` minutes before a minute."""
        events = _match_events(match_id)
        if minute < 1:
            raise ToolError("A possession window needs a minute of at least 1.")
        as_of = _as_of(events, minute)
        start = max(0, minute - window)
        result = possession_window(
            events, PossessionWindowParams(start=start, end=minute), as_of=as_of
        )
        return _payload(match_id, minute, as_of, result)

    @server.tool(name="formation_at", annotations=_READ_ONLY)
    def formation_at_tool(match_id: MatchId, minute: Minute) -> dict[str, Any]:
        """Both teams' formations at a minute, with the event that set each one."""
        events = _match_events(match_id)
        as_of = _as_of(events, minute)
        results = [
            formation_at(events, FormationAtParams(team=team, minute=minute), as_of=as_of)
            for team in ("home", "away")
        ]
        return _payload(match_id, minute, as_of, *results)

    return server


if __name__ == "__main__":
    build_server().run("stdio")
