"""Voice trial: do the configured voices apply their styles, and how long does each line take?

Each line is synthesised once with the tier's style from the voice's own profile and twice
without a style. The MP3 bytes differ between identical requests, so they can't be compared;
the audio duration the service reports is the evidence instead. If the two plain renders last
exactly as long, the voice's timing is deterministic: a styled render of the same length means
the style was ignored, and a different length means the style changed the delivery. If the two
plain renders differ in length, the result is inconclusive.

Every request is timed (wall clock, request to audio). The first request per voice includes
connection set-up and token acquisition, so it is reported separately from the rest.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from azure.core.credentials import TokenCredential

from second_screen_pundit.agents.delivery import TierStyle, voice_profile
from second_screen_pundit.agents.models import DeliveryTier
from second_screen_pundit.agents.speech import SpeechArtifact, SpeechSynthesizer, build_ssml
from second_screen_pundit.agents.spoken import to_spoken

TRIAL_DIR = Path("data/audio/voice-trials")
# Lines from the verified examples in prompts/commentator-persona.md.
TRIAL_LINES: tuple[tuple[str, DeliveryTier, str], ...] = (
    (
        "goal-late-go-ahead",
        "goal_peak",
        "Juno Kestrelane takes a big chance late on, and Ashbridge Rovers are in front. "
        "Ashbridge Rovers lead 3-2.",
    ),
    (
        "goal-equaliser",
        "goal_equaliser",
        "Ivo Juniperby converts a half-chance, and Ashbridge Rovers are back on terms. "
        "The score is level at 2-2.",
    ),
    (
        "goal",
        "goal",
        "Brio Fallowmere finishes it off, and Port Calder have the opener. "
        "Port Calder lead 1-0.",
    ),
    (
        "red-card",
        "red_card",
        "Quen Rillstone is sent off. Port Calder must protect their lead a player short. "
        "Port Calder lead 1-0.",
    ),
    (
        "substitution",
        "substitution",
        "Loran Mallowfen on for Halen Ironsage, as Ashbridge Rovers look for a way back. "
        "Port Calder lead 2-0.",
    ),
    (
        "formation-change",
        "formation",
        "Ashbridge Rovers move from a 4-2-3-1 to a 3-4-3, a back three now as they chase this. "
        "Port Calder lead 2-1.",
    ),
    (
        "tactical-possession",
        "tactical",
        "Ashbridge Rovers have found their passing rhythm: 52.7% of the possession over the "
        "last 10 minutes.",
    ),
)


@dataclass(frozen=True)
class TrialResult:
    voice: str
    line: str
    style: str
    style_degree: float | None
    verdict: str
    styled_file: str | None
    plain_file: str | None
    styled_seconds: float | None = None
    plain_seconds: float | None = None
    plain_repeat_seconds: float | None = None
    # Wall-clock milliseconds per request, in request order: styled, plain, plain repeat.
    request_ms: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class VoiceTiming:
    voice: str
    first_request_ms: int | None
    later_requests: int
    later_median_ms: int | None
    later_slowest_ms: int | None


def _slug(voice: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", voice).strip("-")


def _verdict(plain: float, repeat: float, styled: float) -> str:
    if plain != repeat:
        return "inconclusive: the two plain renders differ in length"
    if styled == plain:
        return "ignored: styled audio lasts exactly as long as plain audio"
    return "applied: styled audio length differs from the repeatable plain length"


RETRY_ATTEMPTS = 5
RETRY_WAIT_SECONDS = 20


def _synthesize(
    synthesizer: SpeechSynthesizer, ssml: str, path: Path, timings: list[int]
) -> SpeechArtifact:
    """Retry with a pause: the trial makes many quick requests and can hit the rate limit.

    Only the successful attempt is timed, so a rate-limit wait doesn't count as latency.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            artifact = synthesizer.synthesize_ssml_mp3(ssml, path)
            timings.append(round((time.perf_counter() - started) * 1000))
            return artifact
        except RuntimeError as exc:
            if attempt == RETRY_ATTEMPTS or "429" not in str(exc):
                raise
            print(f"retrying {path.name} after: {exc}", flush=True)
            time.sleep(RETRY_WAIT_SECONDS)
    raise AssertionError("unreachable")


def _trial(
    synthesizer: SpeechSynthesizer,
    voice: str,
    name: str,
    text: str,
    style: TierStyle | None,
    directory: Path,
) -> TrialResult:
    styled_path = directory / f"{name}.mp3"
    plain_path = directory / f"{name}.plain.mp3"
    repeat_path = directory / f"{name}.plain-repeat.mp3"
    style_name = style.style if style is not None else "none"
    degree = style.style_degree if style is not None else None
    timings: list[int] = []
    spoken = to_spoken(text)
    try:
        styled = _synthesize(
            synthesizer, build_ssml(spoken, voice=voice, style=style), styled_path, timings
        )
        plain = _synthesize(synthesizer, build_ssml(spoken, voice=voice), plain_path, timings)
        repeat = _synthesize(synthesizer, build_ssml(spoken, voice=voice), repeat_path, timings)
        verdict = (
            _verdict(plain.duration_seconds, repeat.duration_seconds, styled.duration_seconds)
            if style is not None
            else "no style: the voice's profile has none for this tier"
        )
        repeat_path.unlink()
        return TrialResult(
            voice, name, style_name, degree, verdict,
            styled_path.as_posix(), plain_path.as_posix(),
            round(styled.duration_seconds, 3), round(plain.duration_seconds, 3),
            round(repeat.duration_seconds, 3), tuple(timings),
        )
    except RuntimeError as exc:
        return TrialResult(
            voice, name, style_name, degree, "error", None, None,
            request_ms=tuple(timings), error=str(exc),
        )


def voice_timings(results: list[TrialResult]) -> list[VoiceTiming]:
    """First request per voice, then the median and slowest of the rest."""
    summary: list[VoiceTiming] = []
    for voice in dict.fromkeys(result.voice for result in results):
        timings = [ms for result in results if result.voice == voice for ms in result.request_ms]
        later = timings[1:]
        summary.append(
            VoiceTiming(
                voice,
                timings[0] if timings else None,
                len(later),
                round(statistics.median(later)) if later else None,
                max(later) if later else None,
            )
        )
    return summary


def run_voice_trial(
    *,
    endpoint: str,
    region: str,
    credential: TokenCredential,
    voices: tuple[str, ...],
    output_dir: Path = TRIAL_DIR,
) -> tuple[list[TrialResult], list[VoiceTiming]]:
    results: list[TrialResult] = []
    for voice in voices:
        # No fallback here: each voice is measured on its own.
        synthesizer = SpeechSynthesizer(
            endpoint=endpoint,
            region=region,
            credential=credential,
            voice_name=voice,
            fallback_voice_name=None,
        )
        profile = voice_profile(voice)
        directory = output_dir / _slug(voice)
        for index, (name, tier, text) in enumerate(TRIAL_LINES, start=1):
            results.append(
                _trial(
                    synthesizer, voice, f"{index:02d}-{name}", text,
                    profile.style_for(tier), directory,
                )
            )
    timings = voice_timings(results)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(
            {
                "results": [asdict(result) for result in results],
                "timings": [asdict(timing) for timing in timings],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return results, timings
