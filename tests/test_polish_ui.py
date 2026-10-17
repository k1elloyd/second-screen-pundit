"""Polish session, group 3: the "Checking the facts…" state and the audio-off label."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from second_screen_pundit.agents.models import Explanation, ExplainerReply
from second_screen_pundit.api import create_app
from second_screen_pundit.engine.generate import generate_match, get_match_info
from tests.test_api import _session, _settings as _api_settings, _SpeechStub, _SynthesisStub
from tests.test_review2_fixes import _alias, _as_of_at, _scripted_pipeline

APP_JS = Path("src/second_screen_pundit/web/app.js").read_text(encoding="utf-8")


def _answers(first_grounded: bool):  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def answer(prompt: dict) -> ExplainerReply:
        calls["n"] += 1
        shots = _alias(prompt, "Merehaven Athletic shots through")
        if calls["n"] == 1 and not first_grounded:
            return ExplainerReply(answer="Merehaven Athletic have had 97 shots.", fact_ids=[])
        return ExplainerReply(
            answer=f"Merehaven Athletic have had {shots['value']} shots.", fact_ids=[shots["id"]]
        )

    return answer


@pytest.mark.parametrize("first_grounded", [True, False])
def test_on_retry_is_called_only_when_the_first_answer_fails_the_evidence_check(
    monkeypatch: pytest.MonkeyPatch, first_grounded: bool
) -> None:
    events, info = generate_match("m02"), get_match_info("m02")
    answer = _answers(first_grounded)
    pipeline, _, _ = _scripted_pipeline(monkeypatch, [answer, answer])
    retries: list[str] = []

    async def on_retry() -> None:
        retries.append("checking")

    result = asyncio.run(
        pipeline.answer_question(
            info, events, "How many shots?", as_of=_as_of_at(events, 74), on_retry=on_retry
        )
    )
    assert result.source == "agent", result.evidence.failures
    assert retries == ([] if first_grounded else ["checking"])


class _RetryingPipeline:
    async def answer_question(self, _info, _events, _question, *, as_of: int, on_retry=None):
        if on_retry is not None:
            await on_retry()
        return SimpleNamespace(explanation=Explanation(answer="Verified after a second look."))


def test_typed_question_gets_answer_checking_on_its_own_question_id() -> None:
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    app = create_app(
        settings=_api_settings(),
        pipeline=_RetryingPipeline(),  # type: ignore[arg-type]
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=_SynthesisStub(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        session = _session(
            "checking-replay", goal_seq=goal.seq, released_as_of=goal.seq,
            client_ip="198.51.100.31",
        )
        app.state.sessions[session.replay_id] = session
        headers = {"X-Forwarded-For": "198.51.100.31", "X-Replay-Token": session.token}
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Who leads?",
                  "question_id": "page0123abcd"},
            headers=headers,
        )

        async def first_item() -> tuple[str, dict]:
            ((_, name, payload), *_) = await session.outbox.read(0, timeout=5)
            return name, payload

        name, payload = client.portal.call(first_item)
        rejected = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Who leads?",
                  "question_id": "<script>"},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["question_id"] == "page0123abcd"
    assert (name, payload) == ("answer_checking", {"question_id": "page0123abcd"})
    assert rejected.status_code == 422


def test_page_shows_checking_the_facts_and_audio_off() -> None:
    assert '"answer_checking"' in APP_JS and "Checking the facts" in APP_JS
    assert "question_id: questionId" in APP_JS
    assert "Audio unavailable · text only" not in APP_JS
    assert APP_JS.count('"Audio off"') == 2
