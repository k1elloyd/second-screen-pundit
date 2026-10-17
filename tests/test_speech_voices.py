"""Voice upgrade: spoken text reaches the SSML, and a failed main voice falls back once."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents import speech
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.spoken import to_spoken


def _fake_sdk(monkeypatch: pytest.MonkeyPatch, failing_voices: set[str]) -> list[str]:
    """A Speech SDK stand-in; any SSML naming a voice in failing_voices is cancelled."""
    sent: list[str] = []

    class FakeSpeechConfig:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def set_speech_synthesis_output_format(self, _output_format: object) -> None:
            pass

    class FakeSpeechSynthesizer:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def speak_ssml_async(self, ssml: str) -> SimpleNamespace:
            sent.append(ssml)
            if any(f'name="{voice}"' in ssml for voice in failing_voices):
                result = SimpleNamespace(
                    reason="canceled",
                    cancellation_details=SimpleNamespace(
                        reason="Error", error_details="Unsupported voice (1007)"
                    ),
                )
            else:
                result = SimpleNamespace(
                    reason="completed",
                    audio_duration=timedelta(seconds=3),
                    audio_data=b"fake-mp3",
                )
            return SimpleNamespace(get=lambda: result)

    monkeypatch.setattr(
        speech,
        "speechsdk",
        SimpleNamespace(
            SpeechConfig=FakeSpeechConfig,
            SpeechSynthesisOutputFormat=SimpleNamespace(Audio24Khz160KBitRateMonoMp3="mp3"),
            SpeechSynthesizer=FakeSpeechSynthesizer,
            ResultReason=SimpleNamespace(SynthesizingAudioCompleted="completed"),
        ),
    )
    return sent


def _synthesizer(voice: str, fallback: str | None = "en-GB-RyanNeural") -> speech.SpeechSynthesizer:
    return speech.SpeechSynthesizer(
        endpoint="https://speech.example.invalid/",
        region="swedencentral",
        credential=object(),  # type: ignore[arg-type]
        voice_name=voice,
        fallback_voice_name=fallback,
    )


def test_synthesis_speaks_the_spoken_text_not_the_displayed_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent = _fake_sdk(monkeypatch, set())
    text = "Port Calder switch to a 4-4-1 at 39'. Port Calder lead 1-0."
    artifact = _synthesizer(speech.DEFAULT_VOICE).synthesize_mp3(
        text, tmp_path / "line.mp3", tier="formation"
    )
    assert len(sent) == 1
    assert to_spoken(text) in sent[0]
    assert "four-four-one" in sent[0] and "one-nil" in sent[0]
    assert "thirty-ninth minute" in sent[0]
    assert "4-4-1" not in sent[0] and "1-0" not in sent[0]
    assert '<mstts:express-as style="narrator">' in sent[0]
    assert artifact.voice == speech.DEFAULT_VOICE


def test_a_failed_main_voice_retries_once_with_the_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = "en-GB-Nobody:MAI-Voice-2.1-Flash"
    sent = _fake_sdk(monkeypatch, {bad})
    with caplog.at_level(logging.INFO, logger=speech.__name__):
        artifact = _synthesizer(bad).synthesize_mp3(
            "Brio Fallowmere scores. Port Calder lead 1-0.", tmp_path / "line.mp3", tier="goal"
        )
    assert len(sent) == 2
    assert f'name="{bad}"' in sent[0]
    assert 'name="en-GB-RyanNeural"' in sent[1]
    # The fallback gets its own style for the tier.
    assert 'style="cheerful"' in sent[1]
    assert artifact.voice == "en-GB-RyanNeural"
    assert artifact.path.read_bytes() == b"fake-mp3"
    messages = [record.getMessage() for record in caplog.records]
    assert any("speech_voice_fallback" in message and bad in message for message in messages)
    assert any("speech_synthesized voice=en-GB-RyanNeural" in message for message in messages)


def test_both_voices_failing_raises_after_one_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent = _fake_sdk(monkeypatch, {"en-GB-Nobody", "en-GB-RyanNeural"})
    with pytest.raises(RuntimeError, match="Speech synthesis failed"):
        _synthesizer("en-GB-Nobody").synthesize_mp3("A line.", tmp_path / "line.mp3")
    assert len(sent) == 2


def test_no_fallback_configured_raises_without_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent = _fake_sdk(monkeypatch, {"en-GB-Nobody"})
    with pytest.raises(RuntimeError):
        _synthesizer("en-GB-Nobody", fallback=None).synthesize_mp3("A line.", tmp_path / "a.mp3")
    assert len(sent) == 1


def test_voice_settings_default_and_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEECH_VOICE", raising=False)
    monkeypatch.delenv("SPEECH_FALLBACK_VOICE", raising=False)
    defaults = AgentSettings.from_environment()
    assert defaults.speech_voice == "en-GB-Harry:MAI-Voice-2.1-Flash"
    assert defaults.speech_fallback_voice == "en-GB-RyanNeural"
    monkeypatch.setenv("SPEECH_VOICE", "en-GB-Emily:MAI-Voice-2.1-Flash")
    monkeypatch.setenv("SPEECH_FALLBACK_VOICE", "en-GB-SoniaNeural")
    configured = AgentSettings.from_environment()
    assert configured.speech_voice == "en-GB-Emily:MAI-Voice-2.1-Flash"
    assert configured.speech_fallback_voice == "en-GB-SoniaNeural"
