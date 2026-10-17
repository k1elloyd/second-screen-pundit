from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import wave
from xml.sax.saxutils import escape, quoteattr

import azure.cognitiveservices.speech as speechsdk
from azure.core.credentials import TokenCredential

from second_screen_pundit.agents.delivery import TIER_STYLES, TierStyle
from second_screen_pundit.agents.models import DeliveryTier

# Phase 2c voice trial: the only en-GB voice whose delivery changed under the sports styles.
DEFAULT_VOICE = "en-GB-RyanNeural"
SSML_LANGUAGE = "en-GB"


@dataclass(frozen=True)
class SpeechArtifact:
    path: Path
    duration_seconds: float
    audio_bytes: int = 0


def build_ssml(
    text: str,
    *,
    voice: str,
    tier: DeliveryTier | None = None,
    style: TierStyle | None = None,
    language: str = SSML_LANGUAGE,
) -> str:
    """Build the SSML document server-side; every inserted value is XML-escaped.

    The tier's style and style degree go in an mstts:express-as element. Azure ignores the
    whole element when the voice doesn't support the style, so speech falls back to the
    voice's neutral delivery instead of failing.
    """
    if not text.strip():
        raise ValueError("Speech text must not be empty")
    chosen = style or (TIER_STYLES[tier] if tier is not None else None)
    body = escape(text)
    if chosen is not None:
        body = (
            f"<mstts:express-as style={quoteattr(chosen.style)} "
            f"styledegree={quoteattr(f'{chosen.style_degree:g}')}>{body}</mstts:express-as>"
        )
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="http://www.w3.org/2001/mstts" '
        f"xml:lang={quoteattr(language)}>"
        f"<voice name={quoteattr(voice)}>{body}</voice>"
        "</speak>"
    )


class SpeechSynthesizer:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        credential: TokenCredential,
        voice_name: str = DEFAULT_VOICE,
    ) -> None:
        if not region.strip():
            raise ValueError("Speech region must not be empty")
        self.voice_name = voice_name
        self._config = speechsdk.SpeechConfig(
            token_credential=credential,
            endpoint=endpoint,
        )
        self._config.speech_synthesis_voice_name = voice_name
        self._config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio24Khz160KBitRateMonoMp3
        )

    def synthesize_mp3(
        self,
        text: str,
        output_path: Path,
        *,
        tier: DeliveryTier | None = None,
    ) -> SpeechArtifact:
        ssml = build_ssml(text, voice=self.voice_name, tier=tier)
        return self.synthesize_ssml_mp3(ssml, output_path)

    def synthesize_ssml_mp3(self, ssml: str, output_path: Path) -> SpeechArtifact:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f"{output_path.stem}.partial.mp3")
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self._config,
                audio_config=None,
            )
            result = synthesizer.speak_ssml_async(ssml).get()
            if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                details = getattr(result, "cancellation_details", None)
                reason = getattr(details, "reason", result.reason)
                error = getattr(details, "error_details", "") or ""
                raise RuntimeError(f"Speech synthesis failed: {reason} {error}".strip())
            duration = result.audio_duration
            if not isinstance(duration, timedelta) or duration.total_seconds() <= 0:
                raise RuntimeError("Speech synthesis returned no positive audio duration")
            audio_data = result.audio_data
            if not isinstance(audio_data, bytes) or not audio_data:
                raise RuntimeError("Speech synthesis completed without MP3 audio data")
            temporary_path.write_bytes(audio_data)
            temporary_path.replace(output_path)
            return SpeechArtifact(output_path, duration.total_seconds(), len(audio_data))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


class SpeechRecognizer:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        credential: TokenCredential,
    ) -> None:
        if not region.strip():
            raise ValueError("Speech region must not be empty")
        self._config = speechsdk.SpeechConfig(
            token_credential=credential,
            endpoint=endpoint,
        )

    def recognize_wav(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            raise ValueError("Question audio must not be empty")
        try:
            with wave.open(BytesIO(audio_bytes), "rb") as wav:
                if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                    raise ValueError("Question audio must be uncompressed 16-bit PCM WAV")
                sample_rate = wav.getframerate()
                channels = wav.getnchannels()
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError, ZeroDivisionError) as exc:
            raise ValueError("Question audio must be a valid WAV file") from exc
        stream_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=sample_rate,
            bits_per_sample=16,
            channels=channels,
        )
        audio_stream = speechsdk.audio.PushAudioInputStream(stream_format=stream_format)
        audio_stream.write(frames)
        audio_stream.close()
        audio_config = speechsdk.AudioConfig(stream=audio_stream)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=self._config,
            audio_config=audio_config,
        )
        result = recognizer.recognize_once_async().get()
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            transcript = result.text.strip()
            if transcript:
                return transcript
            raise RuntimeError("Speech recognition returned an empty transcript")
        if result.reason == speechsdk.ResultReason.NoMatch:
            raise ValueError("No speech was recognized; try recording again")
        details = getattr(result, "cancellation_details", None)
        reason = getattr(details, "reason", result.reason)
        error = getattr(details, "error_details", "") or ""
        raise RuntimeError(f"Speech recognition failed: {reason} {error}".strip())
