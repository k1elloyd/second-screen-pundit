"""Phase 4 group 2: what the container and its infrastructure promise."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from second_screen_pundit.api import create_app
from second_screen_pundit.engine.generate import generate_match
from tests.test_api import _GoalCutoffPipeline, _session, _settings, _SpeechStub, _SynthesisStub

ROOT = Path(__file__).resolve().parents[1]
BICEP = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


def test_healthz_makes_no_model_or_speech_calls() -> None:
    class _Untouchable:
        """Any attribute access means a model or speech call was attempted."""

        def __getattr__(self, name: str):
            raise AssertionError(f"/healthz touched {name}")

    app = create_app(
        settings=_settings(),
        pipeline=_Untouchable(),  # type: ignore[arg-type]
        speech_recognizer=_Untouchable(),  # type: ignore[arg-type]
        speech_synthesizer=_Untouchable(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_a_question_and_its_speech_are_traced(spans: InMemorySpanExporter) -> None:
    spans.clear()
    events = generate_match("m02")
    goal = next(event for event in events if event.type == "goal")
    app = create_app(
        settings=_settings(),
        pipeline=_GoalCutoffPipeline(goal.seq),  # type: ignore[arg-type]
        speech_recognizer=_SpeechStub(),  # type: ignore[arg-type]
        speech_synthesizer=_SynthesisStub(),  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        session = _session(
            "traced-replay", goal_seq=goal.seq, released_as_of=goal.seq, client_ip="198.51.100.70"
        )
        app.state.sessions[session.replay_id] = session
        response = client.post(
            "/api/ask",
            json={"replay_id": session.replay_id, "question": "Has a goal been scored?"},
            headers={"X-Forwarded-For": "198.51.100.70"},
        )
        assert response.status_code == 200

        async def audio_sent() -> None:
            async with asyncio.timeout(5):
                while not any(n == "answer_audio" for _, n, _ in session.outbox.after(0)):
                    await asyncio.sleep(0.02)

        client.portal.call(audio_sent)

    by_name = {span.name: span for span in spans.get_finished_spans()}
    turn = by_name["pundit.turn"]
    assert turn.attributes["app.route"] == "question"
    assert turn.attributes["app.match_id"] == "m02"
    assert turn.attributes["app.as_of"] == goal.seq
    assert turn.attributes["app.evidence.status"] in {"passed", "failed", "none"}
    synth = by_name["pundit.speech.synthesize"]
    assert synth.attributes["app.speech.characters"] == len(response.json()["explanation"]["answer"])
    # No question or answer text in any span (ENABLE_SENSITIVE_DATA stays false).
    for span in spans.get_finished_spans():
        assert "Has a goal been scored?" not in json.dumps(dict(span.attributes or {}))


def test_dockerignore_keeps_secrets_and_local_files_out_of_the_image() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".env", ".venv", "docs/screenshots", ".playwright-mcp", ".azure", ".git"):
        assert entry in ignored or entry.split("/")[0] in ignored, entry


def test_one_replica_and_no_secrets_or_keys_in_the_infrastructure() -> None:
    assert re.search(r"^var maxReplicas = 1$", BICEP, re.M)
    assert re.search(r"^param minReplicas int = 1$", BICEP, re.M)
    for forbidden in ("secrets:", "listKeys", "sharedKey", "secretRef", "apiKey"):
        assert forbidden not in BICEP, forbidden
    assert "DisableLocalAuth: true" in BICEP
    assert "adminUserEnabled: false" in BICEP
    # Roles by GUID, each scoped to its resource.
    for guid in (
        "53ca6127-db72-4b80-b1b0-d745d6d5456d",  # Foundry User
        "f2dc8367-1007-4938-bd23-fe263f013447",  # Cognitive Services Speech User
        "7f951dda-4ed3-4680-a7ca-43fe172d538d",  # AcrPull
        "3913510d-42f4-4e42-8a64-420c390055eb",  # Monitoring Metrics Publisher
    ):
        assert guid in BICEP
    assert "Microsoft.Resources/resourceGroups" not in BICEP  # never creates a resource group


def test_every_setting_the_app_reads_from_env_is_set_in_the_container() -> None:
    env_names = set(re.findall(r"\{ name: '([A-Z_]+)'", BICEP))
    env_names |= set(re.findall(r"name: '([A-Z_]+)'\n\s+value:", BICEP))
    for required in (
        "FOUNDRY_PROJECT_ENDPOINT", "MODEL_FAST", "MODEL_DEEP", "SPEECH_ENDPOINT",
        "SPEECH_REGION", "SPEECH_VOICE", "SPEECH_FALLBACK_VOICE", "AZURE_CLIENT_ID",
        "APPLICATIONINSIGHTS_CONNECTION_STRING", "NARRATIVE_MODE",
    ):
        assert required in env_names, required


def test_observability_is_a_no_op_without_a_connection_string(monkeypatch) -> None:
    import second_screen_pundit.observability as observability

    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(observability, "_configured", False)
    assert observability.configure_observability() is False
