from __future__ import annotations

import json
from collections.abc import Callable, Collection, Sequence
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareFailure

from second_screen_pundit.agents.tools.analytics import (
    ToolResult,
    compare_windows,
    formation_at,
    key_moments,
    match_state,
    pass_network,
    possession_window,
    pressing_intensity,
    shot_summary,
    xg_timeline,
)
from second_screen_pundit.agents.tools.parameters import (
    CompareWindowsParams,
    FormationAtParams,
    KeyMomentsParams,
    MatchStateParams,
    Metric,
    MinuteRange,
    PassNetworkParams,
    PossessionWindowParams,
    PressingIntensityParams,
    ShotSummaryParams,
    Team,
    XgTimelineParams,
)
from second_screen_pundit.engine.schema import Event


ResultMap = Callable[[ToolResult], ToolResult]


class ToolResultCapture(FunctionMiddleware):
    """Captures each tool result for the evidence check, with its real fact IDs restored."""

    def __init__(self, restore: ResultMap | None = None) -> None:
        self.results: list[ToolResult] = []
        self._restore = restore

    async def process(self, context: FunctionInvocationContext, call_next: Any) -> None:
        await call_next()
        result = context.result
        if isinstance(result, ToolResult):
            self.results.append(result)
            return

        contents = result if isinstance(result, list) else [result]
        texts = [
            content.text
            for content in contents
            if isinstance(getattr(content, "text", None), str)
        ]
        if not texts:
            raise MiddlewareFailure("Analytics tool returned no serializable ToolResult")

        for text in texts:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MiddlewareFailure("Analytics tool returned invalid JSON") from exc
            try:
                captured = ToolResult.model_validate(parsed)
            except (TypeError, ValueError) as exc:
                raise MiddlewareFailure("Analytics tool returned an invalid ToolResult") from exc
            function_name = getattr(context.function, "name", None)
            expected_tool = (
                function_name.removesuffix("_tool")
                if isinstance(function_name, str)
                else None
            )
            if expected_tool is not None and captured.tool != expected_tool:
                raise MiddlewareFailure("Analytics tool result does not match its function name")
            self.results.append(self._restore(captured) if self._restore else captured)


def build_analytics_tools(
    events: Sequence[Event],
    *,
    as_of: int,
    exclude: Collection[str] = (),
    present: ResultMap | None = None,
    restore: ResultMap | None = None,
) -> tuple[list[Any], ToolResultCapture]:
    """The analytics tools bound to ``as_of``, and the middleware that captures their results.

    ``exclude`` names tools to leave out. ``present`` rewrites a result for the model (for
    example short fact aliases), and ``restore`` undoes it in the capture.
    """
    if as_of < 0:
        raise ValueError("as_of must be a non-negative event sequence")
    event_log = list(events)
    capture = ToolResultCapture(restore)

    def serialize(result: ToolResult) -> str:
        shown = present(result) if present is not None else result
        return shown.model_dump_json(exclude={"table"})

    def match_state_tool(minute: int) -> str:
        """Return score, possession, shots, xG, cards, and formations at a minute."""
        result = match_state(
            event_log,
            MatchStateParams(minute=minute),
            as_of=as_of,
        )
        return serialize(result)

    def possession_window_tool(start: int, end: int) -> str:
        """Return the home and away possession shares for a minute window."""
        result = possession_window(
            event_log,
            PossessionWindowParams(start=start, end=end),
            as_of=as_of,
        )
        return serialize(result)

    def xg_timeline_tool() -> str:
        """Return cumulative expected goals in five-minute buckets."""
        result = xg_timeline(
            event_log,
            XgTimelineParams(),
            as_of=as_of,
        )
        return serialize(result)

    def shot_summary_tool(team: Team, start: int, end: int) -> str:
        """Return shot count, shots on target, xG, and the best chance for a team."""
        result = shot_summary(
            event_log,
            ShotSummaryParams(team=team, start=start, end=end),
            as_of=as_of,
        )
        return serialize(result)

    def pressing_intensity_tool(team: Team, start: int, end: int) -> str:
        """Return defensive actions per minute and high turnovers for a team."""
        result = pressing_intensity(
            event_log,
            PressingIntensityParams(team=team, start=start, end=end),
            as_of=as_of,
        )
        return serialize(result)

    def pass_network_tool(team: Team, start: int, end: int) -> str:
        """Return progressive passes and the leading passing pairs for a team."""
        result = pass_network(
            event_log,
            PassNetworkParams(team=team, start=start, end=end),
            as_of=as_of,
        )
        return serialize(result)

    def formation_at_tool(team: Team, minute: int) -> str:
        """Return a team's formation and the event that established it."""
        result = formation_at(
            event_log,
            FormationAtParams(team=team, minute=minute),
            as_of=as_of,
        )
        return serialize(result)

    def key_moments_tool(limit: int = 10) -> str:
        """Return the highest-ranked goals, cards, formation changes, and big chances."""
        result = key_moments(
            event_log,
            KeyMomentsParams(limit=limit),
            as_of=as_of,
        )
        return serialize(result)

    def compare_windows_tool(
        team: Team,
        metric: Metric,
        before: MinuteRange,
        after: MinuteRange,
    ) -> str:
        """Compare a team's metric across two minute ranges."""
        result = compare_windows(
            event_log,
            CompareWindowsParams(team=team, metric=metric, before=before, after=after),
            as_of=as_of,
        )
        return serialize(result)

    tools = [
        match_state_tool,
        possession_window_tool,
        xg_timeline_tool,
        shot_summary_tool,
        pressing_intensity_tool,
        pass_network_tool,
        formation_at_tool,
        key_moments_tool,
        compare_windows_tool,
    ]
    return [tool for tool in tools if tool.__name__.removesuffix("_tool") not in exclude], capture
