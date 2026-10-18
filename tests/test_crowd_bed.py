"""Run the page's generated crowd bed and audio queue under Node with fake Web Audio nodes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path("src/second_screen_pundit/web/app.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is needed to run the page's audio")

_HARNESS = r"""
let now = 0;
Date.now = () => now;
const timers = new Map();
let nextTimer = 1;
globalThis.setTimeout = (callback, delay) => {
  const id = nextTimer++;
  timers.set(id, { callback, at: now + delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
function advance(ms) {
  now += ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, timer]) => timer.at <= now)
      .sort((left, right) => left[1].at - right[1].at)[0];
    if (!due) break;
    timers.delete(due[0]);
    due[1].callback();
  }
}
const stored = {};
function readStored(key) { return stored[key] || null; }
function writeStored(key, value) { stored[key] = value; }
function audioUrl(id) { return id; }
function silentAudioUrl() { return "silent"; }
function el() { return { hidden: true }; }
const LINE_AUDIO_STALE_MS = 12000;
class FakeParam {
  constructor(value = 0) { this.value = value; this.events = []; }
  cancelScheduledValues(time) { this.events.push({ kind: "cancel", time }); }
  setTargetAtTime(value, time, constant) {
    this.value = value;
    this.events.push({ kind: "target", value, time, constant });
  }
  setValueAtTime(value, time) {
    this.value = value;
    this.events.push({ kind: "set", value, time });
  }
  linearRampToValueAtTime(value, time) {
    this.value = value;
    this.events.push({ kind: "ramp", value, time });
  }
}
class FakeNode {
  constructor() {
    this.gain = new FakeParam();
    this.frequency = new FakeParam();
    this.Q = new FakeParam();
    this.pan = new FakeParam();
    this.handlers = {};
    this.connections = [];
    this.playCount = 0;
  }
  connect(node) { this.connections.push(node); }
  start(time) { this.startedAt = time; }
  stop(time) { this.stoppedAt = time; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  play() { this.playCount += 1; return Promise.resolve(); }
  pause() {}
}
class FakeAudioContext {
  constructor() {
    this.sampleRate = 100;
    this.currentTime = now / 1000;
    this.state = "running";
    this.destination = new FakeNode();
    this.filters = [];
    this.gains = [];
    this.oscillators = [];
    this.panners = [];
  }
  createBuffer(_channels, length) {
    const samples = new Float32Array(length);
    return { getChannelData: () => samples };
  }
  createBufferSource() { return new FakeNode(); }
  createBiquadFilter() {
    const node = new FakeNode();
    this.filters.push(node);
    return node;
  }
  createGain() {
    const node = new FakeNode();
    this.gains.push(node);
    return node;
  }
  createOscillator() {
    const node = new FakeNode();
    this.oscillators.push(node);
    return node;
  }
  createStereoPanner() {
    const node = new FakeNode();
    this.panners.push(node);
    return node;
  }
  resume() { this.state = "running"; return Promise.resolve(); }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
}
globalThis.window = { AudioContext: FakeAudioContext };
globalThis.Audio = class extends FakeNode {
  constructor() { super(); this.src = ""; }
};
globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };
globalThis.fetch = () => Promise.resolve();
let audioQueue;
let crowdBed;
let state = {
  replayId: null, replayComplete: false, userPaused: false, awayPaused: false, controller: null,
};
function setPauseButton() {}
__CROWD__
__QUEUE__
__HELPERS__
__PAGE_HIDDEN__
(async () => {
  crowdBed = new CrowdBed();
  const bed = crowdBed;
  audioQueue = new AudioQueue();
  const queue = audioQueue;
  queue.onPlaybackChange = (speaking) => bed.setDucked(speaking);
  await bed.startInGesture();
  __ACTIONS__
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def _source_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _run(actions: str) -> dict[str, object]:
    source = APP_JS.read_text(encoding="utf-8")
    crowd = _source_between(source, "const CROWD_BASE_GAIN", "// One <audio> element")
    queue = _source_between(source, "const PRIORITY_AUDIO_TIERS", "const audioQueue = new AudioQueue();")
    helpers = _source_between(source, "function pausePlaybackAudio()", "function startCrowdInGesture({")
    page_hidden = _source_between(source, "let closedWhileHidden = false;", "function pageShown()")
    script = (_HARNESS.replace("__CROWD__", crowd)
              .replace("__QUEUE__", queue)
              .replace("__HELPERS__", helpers)
              .replace("__PAGE_HIDDEN__", page_hidden)
              .replace("__ACTIONS__", actions))
    result = subprocess.run(
        [NODE, "-e", script],  # type: ignore[list-item]
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def test_commentary_queue_preducks_by_12_db_and_holds_after_playback() -> None:
    result = _run("""
      const baseline = bed.output.gain.value;
      queue.enqueue({ id: "line", kind: "line" });
      const beforeClip = {
        playing: queue.el.playCount || 0, ducked: bed.ducked, level: bed.output.gain.value,
        fade: bed.output.gain.events.at(-1).constant,
      };
      advance(299);
      const beforeLeadEnds = queue.el.playCount || 0;
      advance(1);
      const started = queue.el.playCount || 0;
      queue.el.handlers.ended();
      const duringReleaseHold = { ducked: bed.ducked, level: bed.output.gain.value };
      advance(399);
      const beforeRelease = bed.ducked;
      advance(1);
      console.log(JSON.stringify({
        baseline, beforeClip, beforeLeadEnds, started, duringReleaseHold, beforeRelease,
        released: { ducked: bed.ducked, level: bed.output.gain.value,
                   fade: bed.output.gain.events.at(-1).constant },
      }));
    """)
    assert result["baseline"] == pytest.approx(0.04)
    assert result["beforeClip"] == {
        "playing": 0, "ducked": True, "level": pytest.approx(0.01), "fade": 0.08,
    }
    assert result["beforeLeadEnds"] == 0
    assert result["started"] == 1
    assert result["duringReleaseHold"] == {"ducked": True, "level": pytest.approx(0.01)}
    assert result["beforeRelease"] is True
    assert result["released"] == {"ducked": False, "level": pytest.approx(0.04), "fade": 0.14}


def test_goals_swell_for_three_seconds_and_late_goals_swell_more() -> None:
    result = _run("""
      bed.handleEvent({ type: "goal", clock: 74 });
      const goal = bed.output.gain.value;
      advance(1500);
      bed.handleEvent({ type: "goal", clock: 75 });
      const lateGoal = bed.output.gain.value;
      advance(3000);
      console.log(JSON.stringify({ goal, lateGoal, restored: bed.output.gain.value }));
    """)
    assert result["goal"] == pytest.approx(0.09)
    assert result["lateGoal"] == pytest.approx(0.14)
    assert result["restored"] == pytest.approx(0.04)


def test_big_chances_and_red_cards_have_smaller_swells() -> None:
    result = _run("""
      bed.handleEvent({ type: "shot", xg: 0.299 });
      const ordinaryShot = bed.output.gain.value;
      bed.handleEvent({ type: "shot", xg: 0.3 });
      const bigChance = bed.output.gain.value;
      bed.handleEvent({ type: "card", detail: { colour: "red" } });
      const redCard = bed.output.gain.value;
      console.log(JSON.stringify({ ordinaryShot, bigChance, redCard }));
    """)
    assert result["ordinaryShot"] == pytest.approx(0.04)
    assert result["bigChance"] == pytest.approx(0.06)
    assert result["redCard"] == pytest.approx(0.06)


def test_noise_has_four_low_mid_layers_and_slow_stereo_movement() -> None:
    result = _run("""
      console.log(JSON.stringify({
        filters: bed.context.filters.map(filter => ({
          type: filter.type, frequency: filter.frequency.value,
        })),
        modulationHz: bed.context.oscillators.map(node => node.frequency.value),
        panning: bed.context.panners.map(node => node.pan.value),
        crowdOutputConnected: bed.output.connections.includes(bed.context.destination),
        whistleOutputConnected: bed.effectsOutput.connections.includes(bed.context.destination),
        layerGain: bed.context.gains[3].gain.value,
        surgeDepth: bed.context.gains.at(-1).gain.value,
      }));
    """)
    assert result["filters"] == [
        {"type": "highpass", "frequency": 85},
        {"type": "lowpass", "frequency": 400},
        {"type": "highpass", "frequency": 93},
        {"type": "lowpass", "frequency": 470},
        {"type": "highpass", "frequency": 101},
        {"type": "lowpass", "frequency": 540},
        {"type": "highpass", "frequency": 109},
        {"type": "lowpass", "frequency": 610},
    ]
    modulation = result["modulationHz"]
    assert len(modulation) == 5
    assert all(0.018 <= frequency <= 0.038 for frequency in modulation[:4])
    assert modulation[4] == pytest.approx(0.012)
    assert result["panning"] == [-0.6, -0.2, 0.2, 0.6]
    assert result["crowdOutputConnected"] is True
    assert result["whistleOutputConnected"] is True
    assert result["layerGain"] == pytest.approx(0.38)
    assert result["surgeDepth"] == pytest.approx(0.1)


def test_goal_swell_is_held_under_speech_then_returns_after_release() -> None:
    result = _run("""
      bed.handleEvent({ type: "goal", clock: 70 });
      const preSpeech = bed.output.gain.value;
      queue.enqueue({ id: "goal-line", kind: "line" });
      const duringLead = bed.output.gain.value;
      advance(300);
      const duringSpeech = bed.output.gain.value;
      advance(500);
      queue.el.handlers.ended();
      const afterClip = bed.output.gain.value;
      advance(400);
      console.log(JSON.stringify({
        preSpeech, duringLead, duringSpeech, afterClip, afterRelease: bed.output.gain.value,
      }));
    """)
    assert result == {
        "preSpeech": pytest.approx(0.09),
        "duringLead": pytest.approx(0.01),
        "duringSpeech": pytest.approx(0.01),
        "afterClip": pytest.approx(0.01),
        "afterRelease": pytest.approx(0.09),
    }


def test_kickoffs_are_deduplicated_once_per_half() -> None:
    result = _run("""
      bed.handleEvent({ type: "kickoff", period: 1, team: "home" });
      bed.handleEvent({ type: "kickoff", period: 1, team: "away" });
      bed.handleEvent({ type: "kickoff", period: 2, team: "home" });
      bed.handleEvent({ type: "kickoff", period: 2, team: "away" });
      console.log(JSON.stringify({
        periods: [...bed.whistledKickoffs],
        tones: bed.context.oscillators.slice(5).map(node => ({
          frequency: node.frequency.value, duration: node.stoppedAt - node.startedAt,
        })),
      }));
    """)
    assert result["periods"] == [1, 2]
    assert result["tones"] == [
        {"frequency": 1850, "duration": pytest.approx(0.16)},
        {"frequency": 2250, "duration": pytest.approx(0.16)},
        {"frequency": 1850, "duration": pytest.approx(0.16)},
        {"frequency": 2250, "duration": pytest.approx(0.16)},
    ]


@pytest.mark.parametrize(
    ("event_type", "durations"),
    [
        ("half_time", [0.16, 0.16]),
        ("full_time", [0.16, 0.16, 0.42]),
    ],
)
def test_halftime_and_fulltime_whistle_patterns(event_type: str, durations: list[float]) -> None:
    result = _run(f"""
      bed.handleEvent({{ type: "{event_type}" }});
      console.log(JSON.stringify(bed.context.oscillators.slice(5).map(node => ({{
        frequency: node.frequency.value, duration: node.stoppedAt - node.startedAt,
      }}))));
    """)
    assert result == [
        {"frequency": frequency, "duration": pytest.approx(duration)}
        for duration in durations
        for frequency in (1850, 2250)
    ]


def test_mute_and_crowd_toggle_cancel_whistles() -> None:
    result = _run("""
      bed.handleEvent({ type: "kickoff", period: 1 });
      const muteLevels = [...bed.whistles].map(voice => {
        bed.setMuted(true);
        return voice.gain.value;
      });
      bed.setMuted(false);
      bed.handleEvent({ type: "half_time" });
      const toggleVoices = [...bed.whistles];
      bed.setEnabled(false);
      console.log(JSON.stringify({
        muteLevels, muteCleared: bed.whistles.size === 0,
        toggleLevels: toggleVoices.map(voice => voice.gain.value),
        toggleCleared: bed.whistles.size === 0,
      }));
    """)
    assert result == {
        "muteLevels": [0],
        "muteCleared": True,
        "toggleLevels": [0, 0],
        "toggleCleared": True,
    }


def test_pause_and_page_hide_stop_the_crowd_context() -> None:
    result = _run("""
      queue.enqueue({ id: "line", kind: "line" });
      pausePlaybackAudio();
      const paused = { level: bed.output.gain.value, context: bed.context.state };
      await resumePlaybackAudioInGesture();
      state.replayId = "test-replay";
      pageHidden();
      const hidden = { level: bed.output.gain.value, context: bed.context.state };
      console.log(JSON.stringify({ paused, hidden }));
    """)
    for condition in ("paused", "hidden"):
        assert result[condition] == {"level": 0, "context": "suspended"}


def test_mute_and_crowd_toggle_silence_the_bed_and_remember_the_toggle() -> None:
    result = _run("""
      bed.setMuted(true);
      const muted = bed.output.gain.value;
      bed.setMuted(false);
      const unmuted = bed.output.gain.value;
      bed.setEnabled(false);
      const disabled = bed.output.gain.value;
      const storedOff = stored["pundit-crowd-enabled"];
      bed.setEnabled(true);
      const enabled = bed.output.gain.value;
      console.log(JSON.stringify({ muted, unmuted, disabled, storedOff, enabled }));
    """)
    assert result == {
        "muted": 0,
        "unmuted": pytest.approx(0.04),
        "disabled": 0,
        "storedOff": "0",
        "enabled": pytest.approx(0.04),
    }


def test_start_event_mute_and_phone_toggle_are_wired_to_the_bed() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert 'if (name === "event") {\n    renderEvent(payload);\n    crowdBed.handleEvent(payload);' in source
    assert source.count("pausePlaybackAudio();") >= 2
    assert "crowdBed.setMuted(muted);" in source
    assert 'el("crowd-button").addEventListener("click"' in source
    assert "startCrowdInGesture({ starting: true });" in source
