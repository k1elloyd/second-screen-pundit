"""Voice trial: do candidate voices really apply the sports-commentary styles?

Each line is synthesised twice without a style and once with its tier's style. The MP3
bytes differ between identical requests, so they can't be compared; the audio duration the
service reports is the evidence instead. If the two plain renders last exactly as long, the
voice's timing is deterministic: a styled render of the same length means the style was
ignored, and a different length means the style changed the delivery. If the two plain
renders differ in length, the result is inconclusive.

A documented style on a documented voice acts as a positive control for the method.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from azure.core.credentials import TokenCredential

from second_screen_pundit.agents.delivery import TIER_STYLES, TierStyle
from second_screen_pundit.agents.models import DeliveryTier
from second_screen_pundit.agents.speech import SpeechArtifact, SpeechSynthesizer, build_ssml

TRIAL_DIR = Path("data/audio/voice-trials")
CANDIDATE_VOICES = (
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-GB-OllieMultilingualNeural",
    # HD voice; reported as unsupported if the Speech resource's region doesn't offer it.
    "en-GB-Ollie:DragonHDLatestNeural",
)
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
# Positive controls: a documented style on a documented voice, and the only voice Learn
# lists with sports styles (whose table spells them with hyphens).
CONTROLS: tuple[tuple[str, TierStyle], ...] = (
    ("en-GB-RyanNeural", TierStyle("cheerful", 2.0)),
    ("zh-CN-YunjianNeural", TierStyle("sports_commentary_excited", 2.0)),
    ("zh-CN-YunjianNeural", TierStyle("sports-commentary-excited", 2.0)),
)
CONTROL_TEXT = TRIAL_LINES[0][2]


@dataclass(frozen=True)
class TrialResult:
    voice: str
    line: str
    style: str
    style_degree: float
    verdict: str
    styled_file: str | None
    plain_file: str | None
    styled_seconds: float | None = None
    plain_seconds: float | None = None
    plain_repeat_seconds: float | None = None
    error: str | None = None


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


def _synthesize(synthesizer: SpeechSynthesizer, ssml: str, path: Path) -> SpeechArtifact:
    """Retry with a pause: the trial makes many quick requests and can hit the rate limit."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return synthesizer.synthesize_ssml_mp3(ssml, path)
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
    style: TierStyle,
    directory: Path,
) -> TrialResult:
    styled_path = directory / f"{name}.mp3"
    plain_path = directory / f"{name}.plain.mp3"
    repeat_path = directory / f"{name}.plain-repeat.mp3"
    try:
        styled = _synthesize(synthesizer, build_ssml(text, voice=voice, style=style), styled_path)
        plain = _synthesize(synthesizer, build_ssml(text, voice=voice), plain_path)
        repeat = _synthesize(synthesizer, build_ssml(text, voice=voice), repeat_path)
        verdict = _verdict(
            plain.duration_seconds, repeat.duration_seconds, styled.duration_seconds
        )
        repeat_path.unlink()
        return TrialResult(
            voice, name, style.style, style.style_degree, verdict,
            styled_path.as_posix(), plain_path.as_posix(),
            round(styled.duration_seconds, 3), round(plain.duration_seconds, 3),
            round(repeat.duration_seconds, 3),
        )
    except RuntimeError as exc:
        return TrialResult(
            voice, name, style.style, style.style_degree, "error", None, None, error=str(exc)
        )


def run_voice_trial(
    *,
    endpoint: str,
    region: str,
    credential: TokenCredential,
    output_dir: Path = TRIAL_DIR,
) -> list[TrialResult]:
    results: list[TrialResult] = []
    for voice in CANDIDATE_VOICES:
        synthesizer = SpeechSynthesizer(
            endpoint=endpoint, region=region, credential=credential, voice_name=voice
        )
        directory = output_dir / _slug(voice)
        for index, (name, tier, text) in enumerate(TRIAL_LINES, start=1):
            results.append(
                _trial(synthesizer, voice, f"{index:02d}-{name}", text, TIER_STYLES[tier], directory)
            )
    for voice, style in CONTROLS:
        synthesizer = SpeechSynthesizer(
            endpoint=endpoint, region=region, credential=credential, voice_name=voice
        )
        name = f"control-{_slug(style.style)}"
        results.append(
            _trial(synthesizer, voice, name, CONTROL_TEXT, style, output_dir / "controls" / _slug(voice))
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8"
    )
    return results
