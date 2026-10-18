"""Personalisation, group 1: the viewer profile (Mode and My team).

Sent with Start, stored on the replay, changeable mid-match from the next line, and recorded on
each `pundit.turn` span as two fixed attributes with no personal data.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from second_screen_pundit import observability
from second_screen_pundit.agents.dispatcher import LiveDispatcher, WindowOutcome
from second_screen_pundit.agents.pipeline import LiveWindowResult, PunditPipeline
from second_screen_pundit.api import create_app
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.profile import ViewerProfile
from tests.test_api import _settings
from tests.test_stream_resume import (
    _TOKENS,
    _Connection,
    _faster_clock,
    _json_request,
    _scope,
    _stream,
)

WEB = Path(__file__).resolve().parents[1] / "src" / "second_screen_pundit" / "web"


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, *_args: Any) -> None:
        pass


class _Tracer:
    """Records every span's attributes, in place of the real tracer."""

    def __init__(self) -> None:
        self.spans: list[_Span] = []

    @contextmanager
    def start_as_current_span(self, name: str, **_kwargs: Any):
        span = _Span(name)
        self.spans.append(span)
        yield span

    def turns(self, route: str) -> list[dict[str, Any]]:
        return [
            span.attributes
            for span in self.spans
            if span.name == "pundit.turn" and span.attributes.get("app.route") == route
        ]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> _Tracer:
    tracer = _Tracer()
    monkeypatch.setattr(observability, "tracer", tracer)
    return tracer


def _app():
    settings = _settings()
    return create_app(settings=settings, pipeline=PunditPipeline(settings, None))


def _start(client: TestClient, query: str = "", client_id: str = "profile-viewer"):
    return client.post(
        f"/api/matches/m01/start?speed=20{query}", headers={"X-Client-ID": client_id}
    )


def test_start_stores_the_profile_and_defaults_to_casual_neutral() -> None:
    app = _app()
    with TestClient(app) as client:
        default = _start(client, client_id="profile-default")
        chosen = _start(client, "&mode=analyst&team=away", client_id="profile-chosen")
        assert default.status_code == 200 and chosen.status_code == 200
        assert default.json()["profile"] == {"mode": "casual", "team": "neutral"}
        assert chosen.json()["profile"] == {"mode": "analyst", "team": "away"}
        session = app.state.sessions[chosen.json()["replay_id"]]
        assert session.profile == ViewerProfile(mode="analyst", team="away")


@pytest.mark.parametrize("query", ["&mode=expert", "&team=ashbridge", "&team="])
def test_start_refuses_an_unknown_profile_value(query: str) -> None:
    with TestClient(_app()) as client:
        assert _start(client, query).status_code == 422


def test_the_profile_changes_mid_match_with_the_replay_token_only() -> None:
    app = _app()
    with TestClient(app) as client:
        started = _start(client).json()
        replay_id = started["replay_id"]
        url = f"/api/replays/{replay_id}/profile"
        body = {"mode": "analyst", "team": "home"}

        assert client.post(url, json=body).status_code == 404
        assert client.post(url, json=body, headers={"X-Replay-Token": "wrong"}).status_code == 404
        token = {"X-Replay-Token": started["replay_token"]}
        assert client.post(url, json={"mode": "pro", "team": "home"}, headers=token).status_code == 422
        changed = client.post(url, json=body, headers=token)
        assert changed.status_code == 200
        assert changed.json()["profile"] == body
        assert app.state.sessions[replay_id].profile == ViewerProfile("analyst", "home")


class _SkippingPipeline:
    async def comment_on_window(self, *_args: Any, **_kwargs: Any) -> LiveWindowResult:
        return LiveWindowResult("skipped", None, None, (), None)


def test_each_live_window_reads_the_profile_when_it_runs(spans: _Tracer) -> None:
    events = generate_match("m01")
    current = {"profile": ViewerProfile()}
    dispatcher = LiveDispatcher(
        _SkippingPipeline(),  # type: ignore[arg-type]
        get_match_info("m01"),
        events,
        _settings(),
        profile=lambda: current["profile"],
    )

    async def run() -> None:
        await dispatcher._run(WindowOutcome("tactical", events[200], 10), [events[200]], (), None)
        current["profile"] = ViewerProfile(mode="analyst", team="away")  # changed mid-match
        await dispatcher._run(WindowOutcome("tactical", events[400], 20), [events[400]], (), None)

    asyncio.run(run())
    turns = spans.turns("live")
    assert [(t["app.profile.mode"], t["app.profile.team"]) for t in turns] == [
        ("casual", "neutral"),
        ("analyst", "away"),
    ]


def test_question_turns_carry_the_profile_and_no_personal_data(spans: _Tracer) -> None:
    app = _app()
    with TestClient(app) as client:
        started = _start(client, "&mode=analyst&team=home").json()
        session = app.state.sessions[started["replay_id"]]
        session.viewer_released_as_of = 300  # the viewer has seen part of the match
        answered = client.post(
            "/api/ask",
            json={"replay_id": started["replay_id"], "question": "Who is on top?"},
            headers={"X-Replay-Token": started["replay_token"]},
        )
        assert answered.status_code == 200
    (turn,) = spans.turns("question")
    assert turn["app.profile.mode"] == "analyst" and turn["app.profile.team"] == "home"
    # Two fixed choices only: nothing about the viewer themselves.
    profile_keys = {key for key in turn if key.startswith("app.profile.")}
    assert profile_keys == {"app.profile.mode", "app.profile.team"}
    assert not any("client" in key or "ip" in key.split(".") for key in turn)


def test_the_profile_survives_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 10)
    app = _app()
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    async def run() -> None:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.180"
            started = await routes["/api/matches/{match_id}/start"](
                "m01",
                Request(_scope("/api/matches/m01/start", ip)),
                speed=120,
                mode="analyst",
                team="away",
            )
            replay_id = started["replay_id"]
            _TOKENS[replay_id] = started["replay_token"]
            first = _Connection(await _stream(routes, ip, replay_id))
            received = await first.read(lambda got: len(got) >= 20)
            await first.drop()
            second = _Connection(await _stream(routes, ip, replay_id, str(received[-1][0])))
            # The drop paused the match; the page resumes it after reconnecting.
            await routes["/api/replays/{replay_id}/control"](
                replay_id, _json_request({"action": "resume"}, ip, replay_id)
            )
            await second.read(lambda got: len(got) >= 5)
            assert app.state.sessions[replay_id].profile == ViewerProfile("analyst", "away")
            await second.drop()

    asyncio.run(asyncio.wait_for(run(), 30))


def test_the_page_offers_the_profile_before_start_and_in_match_info() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    # In the control bar (before Start; the whole match on the desktop) and in Match info
    # (phones, while a match runs).
    assert html.count('data-profile="mode"') == 2 and html.count('data-profile="team"') == 2
    assert 'id="mode-picker"' in html and 'id="team-picker"' in html
    assert '<option value="casual" selected>' in html and '<option value="neutral" selected>' in html
    # Kept per viewer in sessionStorage, sent with Start, and posted when changed mid-match.
    assert 'const PROFILE_KEY = "pundit-profile"' in script
    assert "sessionStorage.setItem(PROFILE_KEY" in script
    assert "mode: state.profile.mode, team: state.profile.team" in script
    assert "/profile`" in script
