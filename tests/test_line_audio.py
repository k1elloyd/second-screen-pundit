"""Line audio is made during the broadcast delay, and the text never waits for it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request

from second_screen_pundit.agents.models import CommentaryLine
from second_screen_pundit.agents.pipeline import LiveWindowResult
from second_screen_pundit.api import create_app
from second_screen_pundit.engine.generate import generate_match
from tests.test_api import _settings


class _GoalLinePipeline:
    """Writes one line for the goal window and nothing else; no model calls."""

    async def warm_up(self) -> None:
        return None

    async def comment_on_window(self, _info, _events, window, *, as_of, **_kwargs):
        goal = next((event for event in window if event.type == "goal"), None)
        if goal is None:
            return LiveWindowResult("skipped", None, None, (), None)
        line = CommentaryLine(
            line_id="goal-line",
            text="Port Calder lead 1-0.",
            insight_id="goal-insight",
            tone="urgent",
            delivery_tier="goal",
            event_ids=[goal.event_id],
        )
        return LiveWindowResult("commentary", line, None, (), None)

    async def produce_recap(self, *_args, **_kwargs):
        raise RuntimeError("no recap in this test")


class _Synth:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.texts: list[str] = []

    def synthesize_mp3(self, text: str, path: Path, *, tier=None):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("Speech synthesis failed: Unsupported voice")
        path.write_bytes(b"line-mp3")
        return SimpleNamespace(path=path)


def _short_match(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m02")
    kickoff = next(event for event in events if event.type == "kickoff")
    goal = next(event for event in events if event.type == "goal")
    full_time = next(event for event in events if event.type == "full_time")
    short = [
        kickoff,
        goal.model_copy(update={"seq": kickoff.seq + 1, "minute": 0, "second": 1}),
        full_time.model_copy(update={"seq": kickoff.seq + 2, "minute": 0, "second": 2}),
    ]
    monkeypatch.setattr("second_screen_pundit.api.generate_match", lambda _match_id: short)


def _run(synth: _Synth, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    _short_match(monkeypatch)
    app = create_app(
        settings=_settings(delay=0.5),
        pipeline=_GoalLinePipeline(),  # type: ignore[arg-type]
        speech_recognizer=object(),  # type: ignore[arg-type]
        speech_synthesizer=synth,  # type: ignore[arg-type]
    )

    async def consume() -> list[tuple[str, dict]]:
        async with app.router.lifespan_context(app):
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/api/matches/m02/start",
                "raw_path": b"/api/matches/m02/start", "query_string": b"",
                "headers": [(b"x-tab-id", b"line-audio-tab")],
                "client": ("198.51.100.40", 1234), "server": ("127.0.0.1", 8000),
            }
            routes = {
                route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")
            }
            started = await routes["/api/matches/{match_id}/start"]("m02", Request(scope), speed=120)
            response = await routes["/api/replays/{replay_id}/stream"](started["replay_id"])
            received: list[tuple[str, dict]] = []
            buffer = ""
            async for chunk in response.body_iterator:
                buffer += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    lines = dict(
                        line.split(": ", 1) for line in block.splitlines() if ": " in line
                    )
                    if "event" in lines:
                        received.append((lines["event"], json.loads(lines["data"])))
                if any(name == "complete" for name, _ in received):
                    break
            await response.body_iterator.aclose()
            return received

    return asyncio.run(asyncio.wait_for(consume(), timeout=20))


def test_line_audio_is_ready_when_the_line_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    synth = _Synth()
    received = _run(synth, monkeypatch)
    commentary = [payload for name, payload in received if name == "commentary"]
    assert len(commentary) == 1
    # Synthesised during the 0.5 s delay, so text and audio arrive in one event.
    assert commentary[0]["audio_id"] is not None
    assert commentary[0]["audio_available"] is True
    assert commentary[0]["audio_pending"] is False
    assert not any(name.startswith("commentary_audio") for name, _ in received)
    assert synth.texts == ["Port Calder lead 1-0."]


def test_the_line_text_is_delivered_when_its_audio_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    received = _run(_Synth(fail=True), monkeypatch)
    names = [name for name, _ in received]
    commentary = [payload for name, payload in received if name == "commentary"]
    assert len(commentary) == 1
    assert commentary[0]["line"]["text"] == "Port Calder lead 1-0."
    assert commentary[0]["audio_id"] is None
    assert commentary[0]["audio_available"] is False
    assert "commentary_audio" not in names
