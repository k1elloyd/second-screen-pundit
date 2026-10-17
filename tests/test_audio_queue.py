"""Polish session, group 3: the page's audio queue keeps every goal, red-card and late-drama clip.

The real AudioQueue class is cut out of web/app.js and run under Node with a fake <audio>
element and a simulated clock, so the test exercises the shipped code, not a copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path("src/second_screen_pundit/web/app.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is needed to run the page's queue")

_HARNESS = r"""
let now = 0;
Date.now = () => now;
const played = [];
class FakeAudio {
  constructor() { this.handlers = {}; this.src = ""; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  play() { played.push(this.src); return Promise.resolve(); }
  pause() {}
}
globalThis.Audio = FakeAudio;
globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };
function el() { return { hidden: true }; }
function readStored() { return null; }
function writeStored() {}
function audioUrl(id) { return id; }
function silentAudioUrl() { return "silent"; }
const LINE_AUDIO_STALE_MS = 12000;
__QUEUE__
const scenario = JSON.parse(process.argv[process.argv.length - 1]);
const queue = new AudioQueue();
const arrivals = scenario.lines.map((line, index) => ({ ...line, at: index * scenario.gap_ms }));
let endsAt = null;
let playingCount = 0;
while (arrivals.length || endsAt !== null) {
  const nextArrival = arrivals.length ? arrivals[0].at : Infinity;
  if (endsAt !== null && endsAt <= nextArrival) {
    now = endsAt;
    endsAt = null;
    queue.el.handlers.ended();
  } else {
    now = nextArrival;
    const line = arrivals.shift();
    queue.enqueue({ id: line.id, kind: "line", lineId: line.id, tier: line.tier,
                    expiresAt: now + LINE_AUDIO_STALE_MS });
  }
  if (played.length > playingCount) {
    playingCount = played.length;
    endsAt = now + scenario.clip_ms;
  }
}
console.log(JSON.stringify(played));
"""


def _queue_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const PRIORITY_AUDIO_TIERS")
    end = source.index("const audioQueue = new AudioQueue();")
    return source[start:end]


def _play(lines: list[tuple[str, str]], *, gap_ms: int = 1000, clip_ms: int = 15000) -> list[str]:
    scenario = {
        "lines": [{"id": line_id, "tier": tier} for line_id, tier in lines],
        "gap_ms": gap_ms,
        "clip_ms": clip_ms,
    }
    script = _HARNESS.replace("__QUEUE__", _queue_source())
    output = subprocess.run(
        [NODE, "-e", script, json.dumps(scenario)],  # type: ignore[list-item]
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert output.returncode == 0, output.stderr[-2000:]
    return json.loads(output.stdout)


@pytest.mark.parametrize("position", range(5))
@pytest.mark.parametrize("tier", ["goal", "goal_peak", "goal_equaliser", "red_card", "big_chance"])
def test_in_a_burst_of_five_the_goal_clip_always_plays(position: int, tier: str) -> None:
    lines = [(f"l{index}", "tactical") for index in range(5)]
    lines[position] = ("goal", tier)
    played = _play(lines)
    assert "goal" in played, played


def test_two_priority_clips_in_a_burst_both_play_in_order() -> None:
    played = _play(
        [("l0", "tactical"), ("goal", "goal"), ("l2", "substitution"), ("red", "red_card"),
         ("l4", "routine")]
    )
    assert [item for item in played if item in {"goal", "red"}] == ["goal", "red"]


def test_routine_clips_are_dropped_oldest_first() -> None:
    played = _play([(f"l{index}", "tactical") for index in range(5)], clip_ms=4500)
    # l0 plays at once; l1-l4 arrive while it plays and only the newest waiting one is kept.
    assert played[0] == "l0"
    assert "l4" in played
    assert "l1" not in played and "l2" not in played


def test_a_stale_routine_clip_is_skipped_but_a_late_goal_is_not() -> None:
    # Each clip is 15 s, so anything waiting behind one clip is past the 12 s limit.
    assert _play([("l0", "tactical"), ("l1", "tactical")]) == ["l0"]
    assert _play([("l0", "tactical"), ("goal", "goal")]) == ["l0", "goal"]
