"""The local stdio MCP server: the analytics tools, their cut-off and their errors."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from second_screen_pundit.agents.tools.analytics import (
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
from second_screen_pundit.engine.generate import generate_match
from second_screen_pundit.mcp_server import build_server

TOOLS = {"list_matches", "match_state", "key_moments", "possession_window", "formation_at"}
REPO = Path(__file__).resolve().parents[1]


def _call(name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    async def run() -> tuple[bool, Any]:
        async with create_connected_server_and_client_session(build_server()) as session:
            result = await session.call_tool(name, arguments)
        text = result.content[0].text
        return bool(result.isError), text if result.isError else json.loads(text)

    return asyncio.run(run())


def _ok(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    is_error, payload = _call(name, arguments)
    assert not is_error, payload
    return payload


def _as_of(events, minute: int) -> int:
    return max(e.seq for e in events if e.minute + e.added_minute <= minute)


def _facts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [fact for result in payload["results"] for fact in result["facts"]]


def _comparable(facts) -> list[tuple]:
    """Facts without the per-call UUID in their IDs: tool, label, value and events."""
    rows = []
    for fact in facts:
        fact = fact if isinstance(fact, dict) else fact.model_dump()
        tool, _uuid, slug = fact["fact_id"].split(":", 2)
        rows.append((tool, slug, fact["label"], fact["value"], tuple(fact["event_ids"])))
    return rows


def test_the_five_tools_are_listed_and_read_only() -> None:
    async def run():
        async with create_connected_server_and_client_session(build_server()) as session:
            return (await session.list_tools()).tools

    tools = asyncio.run(run())
    assert {tool.name for tool in tools} == TOOLS
    assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)


def test_list_matches_names_the_three_fixtures() -> None:
    matches = _ok("list_matches", {})["matches"]
    assert [m["id"] for m in matches] == ["m01", "m02", "m03"]
    assert matches[0]["label"] == "Ashbridge Rovers v Port Calder FC"


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
@pytest.mark.parametrize("minute", [10, 45, 70, 90])
def test_each_tool_returns_the_analytics_facts_at_the_minute(match_id: str, minute: int) -> None:
    events = generate_match(match_id)
    as_of = _as_of(events, minute)
    expected = {
        "match_state": [match_state(events, MatchStateParams(minute=minute), as_of=as_of)],
        "key_moments": [key_moments(events, KeyMomentsParams(limit=10), as_of=as_of)],
        "possession_window": [
            possession_window(
                events, PossessionWindowParams(start=minute - 10, end=minute), as_of=as_of
            )
        ],
        "formation_at": [
            formation_at(events, FormationAtParams(team=team, minute=minute), as_of=as_of)
            for team in ("home", "away")
        ],
    }
    arguments = {
        "match_state": {"match_id": match_id, "minute": minute},
        "key_moments": {"match_id": match_id, "up_to_minute": minute},
        "possession_window": {"match_id": match_id, "minute": minute, "window": 10},
        "formation_at": {"match_id": match_id, "minute": minute},
    }
    for name, results in expected.items():
        payload = _ok(name, arguments[name])
        assert payload["as_of"] == as_of
        assert _comparable(_facts(payload)) == _comparable(
            [fact for result in results for fact in result.facts]
        ), name
        # Each fact ID has the shape the evidence check reads: tool:<uuid>:<label>.
        assert all(fact["fact_id"].startswith(f"{name}:") for fact in _facts(payload))


def test_known_m01_facts() -> None:
    state = {f["label"]: f["value"] for f in _facts(_ok("match_state", {"match_id": "m01", "minute": 30}))}
    assert state["Match-level score through 30'"] == "0-1"
    moments = _ok("key_moments", {"match_id": "m01", "up_to_minute": 30})
    assert [row["label"] for row in moments["results"][0]["facts"]] == [
        "Port Calder FC goal at 23'",
        "Port Calder FC big chance at 23'",
    ]
    shares = [f["value"] for f in _facts(_ok("possession_window", {"match_id": "m01", "minute": 30}))]
    assert round(sum(shares), 1) == 100.0


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_nothing_after_the_requested_minute_is_returned(match_id: str) -> None:
    clock = {e.event_id: e.minute + e.added_minute for e in generate_match(match_id)}
    for minute in range(5, 91, 5):
        for name, arguments in (
            ("match_state", {"match_id": match_id, "minute": minute}),
            ("key_moments", {"match_id": match_id, "up_to_minute": minute, "limit": 25}),
            ("possession_window", {"match_id": match_id, "minute": minute, "window": 15}),
            ("formation_at", {"match_id": match_id, "minute": minute}),
        ):
            payload = _ok(name, arguments)
            cited = [event_id for fact in _facts(payload) for event_id in fact["event_ids"]]
            assert all(clock[event_id] <= minute for event_id in cited), (name, minute)
            for result in payload["results"]:
                for row in result.get("table") or []:
                    assert row.get("minute", 0) <= minute, (name, minute, row)


def test_a_later_goal_is_not_visible_before_it_happens() -> None:
    before = _facts(_ok("match_state", {"match_id": "m01", "minute": 22}))
    assert {f["label"]: f["value"] for f in before}["Match-level score through 22'"] == "0-0"
    moments = _ok("key_moments", {"match_id": "m01", "up_to_minute": 22})
    assert not any("goal" in f["label"] for f in _facts(moments))


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("match_state", {"match_id": "m99", "minute": 30}, "Unknown match 'm99'"),
        ("formation_at", {"match_id": "", "minute": 30}, "Unknown match"),
        ("match_state", {"match_id": "m01", "minute": 91}, "less than or equal to 90"),
        ("key_moments", {"match_id": "m01", "up_to_minute": -1}, "greater than or equal to 0"),
        ("possession_window", {"match_id": "m01", "minute": 0}, "at least 1"),
        ("possession_window", {"match_id": "m01", "minute": 30, "window": 0}, "greater than or equal to 1"),
        ("match_state", {"match_id": "m01"}, "minute"),
        ("match_state", {"match_id": "m01", "minute": "soon"}, "minute"),
    ],
)
def test_invalid_matches_and_minutes_are_refused_cleanly(
    name: str, arguments: dict[str, Any], message: str
) -> None:
    is_error, text = _call(name, arguments)
    assert is_error
    assert message in text
    assert "Traceback" not in text


def test_the_server_loads_no_model_speech_or_web_app_code() -> None:
    code = (
        "import sys, second_screen_pundit.mcp_server as m; m.build_server(); "
        "bad = [n for n in sys.modules if n.split('.')[0] in ('agent_framework', 'azure', 'openai', 'fastapi') "
        "or n in ('second_screen_pundit.api', 'second_screen_pundit.agents.pipeline', "
        "'second_screen_pundit.agents.speech', 'second_screen_pundit.agents.clients')]; print(bad)"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=REPO
    ).stdout.strip()
    assert loaded == "[]"


def test_the_stdio_server_starts_and_lists_its_tools() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "second_screen_pundit.mcp_server"], cwd=REPO
    )

    async def run() -> set[str]:
        async with asyncio.timeout(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return {tool.name for tool in (await session.list_tools()).tools}

    assert asyncio.run(run()) == TOOLS
