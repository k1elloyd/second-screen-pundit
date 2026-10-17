from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
import logging
from pathlib import Path
import wave
from xml.sax.saxutils import escape, quoteattr

import azure.cognitiveservices.speech as speechsdk
from azure.core.credentials import TokenCredential

from second_screen_pundit.agents.config import (
    DEFAULT_SPEECH_FALLBACK_VOICE,
    DEFAULT_SPEECH_VOICE,
)
from second_screen_pundit.agents.delivery import TierStyle, voice_profile
from second_screen_pundit.agents.models import DeliveryTier
from second_screen_pundit.agents.spoken import to_spoken

logger = logging.getLogger(__name__)

# Phase 3 Session B: MAI voices need a resource in a MAI region (Sweden Central here).
DEFAULT_VOICE = DEFAULT_SPEECH_VOICE
DEFAULT_FALLBACK_VOICE = DEFAULT_SPEECH_FALLBACK_VOICE
SSML_LANGUAGE = "en-GB"


@dataclass(frozen=True)
class SpeechArtifact:
    path: Path
    duration_seconds: float
    audio_bytes: int = 0
    voice: str = ""


def build_ssml(
    text: str,
    *,
    voice: str,
    tier: DeliveryTier | None = None,
    style: TierStyle | None = None,
    language: str = SSML_LANGUAGE,
) -> str:
    """Build the SSML document server-side; every inserted value is XML-escaped.

    The tier's style comes from the voice's own profile and is sent only when the voice
    supports it; otherwise no express-as element is sent at all. An explicit `style` (the
    voice trial) is sent as given.
    """
    if not text.strip():
        raise ValueError("Speech text must not be empty")
    chosen = style or voice_profile(voice).style_for(tier)
    body = escape(text)
    if chosen is not None:
        degree = (
            f" styledegree={quoteattr(f'{chosen.style_degree:g}')}"
            if chosen.style_degree is not None
            else ""
        )
        body = (
            f"<mstts:express-as style={quoteattr(chosen.style)}{degree}>"
            f"{body}</mstts:express-as>"
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
        fallback_voice_name: str | None = DEFAULT_FALLBACK_VOICE,
    ) -> None:
        if not region.strip():
            raise ValueError("Speech region must not be empty")
        self.voice_name = voice_name
        self.fallback_voice_name = (
            fallback_voice_name if fallback_voice_name != voice_name else None
        )
        self._config = speechsdk.SpeechConfig(
            token_credential=credential,
            endpoint=endpoint,
        )
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
        """Speak the line with the main voice, retrying once with the fallback voice.

        The text is rewritten for speech here and nowhere else; the displayed text is unchanged.
        """
        spoken = to_spoken(text)
        try:
            artifact = self.synthesize_ssml_mp3(
                build_ssml(spoken, voice=self.voice_name, tier=tier), output_path
            )
        except RuntimeError as exc:
            if self.fallback_voice_name is None:
                raise
            logger.warning(
                "speech_voice_fallback voice=%s fallback=%s error=%s",
                self.voice_name,
                self.fallback_voice_name,
                str(exc)[:200],
            )
            artifact = self.synthesize_ssml_mp3(
                build_ssml(spoken, voice=self.fallback_voice_name, tier=tier), output_path
            )
            artifact = SpeechArtifact(
                artifact.path,
                artifact.duration_seconds,
                artifact.audio_bytes,
                self.fallback_voice_name,
            )
        else:
            artifact = SpeechArtifact(
                artifact.path, artifact.duration_seconds, artifact.audio_bytes, self.voice_name
            )
        style = voice_profile(artifact.voice).style_for(tier)
        logger.info(
            "speech_synthesized voice=%s tier=%s style=%s",
            artifact.voice,
            tier,
            style.style if style is not None else "none",
        )
        return artifact

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
