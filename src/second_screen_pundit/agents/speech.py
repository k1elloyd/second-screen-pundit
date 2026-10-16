from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import azure.cognitiveservices.speech as speechsdk
from azure.core.credentials import TokenCredential


@dataclass(frozen=True)
class SpeechArtifact:
    path: Path
    duration_seconds: float


class SpeechSynthesizer:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        credential: TokenCredential,
        voice_name: str = "en-GB-SoniaNeural",
    ) -> None:
        if not region.strip():
            raise ValueError("Speech region must not be empty")
        self._config = speechsdk.SpeechConfig(
            token_credential=credential,
            endpoint=endpoint,
        )
        self._config.speech_synthesis_voice_name = voice_name
        self._config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio24Khz160KBitRateMonoMp3
        )

    def synthesize_mp3(self, text: str, output_path: Path) -> SpeechArtifact:
        if not text.strip():
            raise ValueError("Speech text must not be empty")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f"{output_path.stem}.partial.mp3")
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self._config,
            )
            result = synthesizer.speak_text_async(text).get()
            if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                details = speechsdk.SpeechSynthesisCancellationDetails.from_result(result)
                reason = getattr(details, "reason", "unknown")
                raise RuntimeError(f"Speech synthesis failed: {reason}")
            duration = result.audio_duration
            if not isinstance(duration, timedelta) or duration.total_seconds() <= 0:
                raise RuntimeError("Speech synthesis returned no positive audio duration")
            audio_data = result.audio_data
            if not isinstance(audio_data, bytes) or not audio_data:
                raise RuntimeError("Speech synthesis completed without MP3 audio data")
            temporary_path.write_bytes(audio_data)
            temporary_path.replace(output_path)
            return SpeechArtifact(output_path, duration.total_seconds())
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
