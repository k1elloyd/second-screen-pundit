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
    this.playbackRate = new FakeParam(1);
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
    this.convolvers = [];
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
  createConvolver() {
    const node = new FakeNode();
    this.convolvers.push(node);
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
// Renders silence of the right length; a test can replace `fill` to give it content.
class FakeOfflineAudioContext extends FakeAudioContext {
  constructor(channels, length, rate) {
    super();
    this.sampleRate = rate;
    this.length = length;
    this.channels = channels;
  }
  startRendering() {
    const buffer = this.createBuffer(this.channels, this.length, this.sampleRate);
    FakeOfflineAudioContext.fill(buffer.getChannelData(0));
    return Promise.resolve(buffer);
  }
}
FakeOfflineAudioContext.fill = () => {};
globalThis.window = { AudioContext: FakeAudioContext, OfflineAudioContext: FakeOfflineAudioContext };
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
// Wait for a promise whose work pauses on (fake) timers, stepping the clock meanwhile.
async function settle(promise) {
  let done = false;
  let value;
  promise.then((result) => { done = true; value = result; });
  while (!done) { advance(0); await new Promise((resolve) => setImmediate(resolve)); }
  return value;
}
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
  // The loops are built with timers between their parts: step the fake clock.
  await settle(bed.loopsReady);
  // Oscillators made after this point are the whistles.
  const crowdOscillators = bed.context.oscillators.length;
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
    # Through stdin: the script is longer than a Windows command line allows.
    result = subprocess.run(
        [NODE, "-"],  # type: ignore[list-item]
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def test_the_scene_has_24_voices_with_two_formants_spread_in_stereo_and_a_rumble() -> None:
    result = _run("""
      const context = new FakeOfflineAudioContext(2, 16000, CROWD_RENDER_RATE);
      const scene = buildCrowdScene(
        context, context.destination, [...Array(CROWD_VOICES).keys()], 30);
      const bandpass = context.filters.filter(filter => filter.type === "bandpass");
      const lowpass = context.filters.filter(filter => filter.type === "lowpass");
      const firstValue = param => param.events.find(event => event.kind === "set").value;
      console.log(JSON.stringify({
        voices: scene.voices.length,
        bandpass: bandpass.length,
        formantOne: scene.voices.map(voice => firstValue(voice.formantOne.frequency)),
        formantTwo: scene.voices.map(voice => firstValue(voice.formantTwo.frequency)),
        rumble: lowpass.map(filter => filter.frequency.value),
        panning: scene.voices.map(voice => voice.panner.pan.value),
      }));
    """)
    assert result["voices"] == 24
    assert result["bandpass"] == 48
    assert all(300 <= frequency <= 800 for frequency in result["formantOne"])
    assert all(900 <= frequency <= 2500 for frequency in result["formantTwo"])
    # The rumble: one low-pass under 150 Hz.
    assert len(result["rumble"]) == 1 and result["rumble"][0] < 150
    panning = sorted(result["panning"])
    assert all(abs(pan) <= 0.9 for pan in panning)
    assert panning[0] < -0.75 and panning[-1] > 0.75
    # Evenly spread: no gap between neighbouring voices wider than two slots.
    assert max(right - left for left, right in zip(panning, panning[1:])) < 4 * 0.9 / 24


def test_each_voice_pulses_in_whole_syllables_at_three_to_six_per_second() -> None:
    result = _run("""
      const context = new FakeOfflineAudioContext(2, 16000, CROWD_RENDER_RATE);
      const voices = [];
      for (let index = 0; index < 20; index += 1) {
        const samples = crowdVoiceBuffer(context).getChannelData(0);
        // Count the bursts between silent runs, in windows of 1 ms (16 samples).
        let bursts = 0;
        let inBurst = false;
        for (let start = 0; start + 16 <= samples.length; start += 16) {
          let peak = 0;
          for (let offset = 0; offset < 16; offset += 1) {
            peak = Math.max(peak, Math.abs(samples[start + offset]));
          }
          if (peak > 0.02 && !inBurst) { bursts += 1; inBurst = true; }
          if (peak <= 0.02) inBurst = false;
        }
        voices.push({
          seconds: samples.length / CROWD_RENDER_RATE,
          first: samples[0], last: samples.at(-1), bursts,
        });
      }
      console.log(JSON.stringify(voices));
    """)
    for voice in result:
        assert 3.5 <= voice["seconds"] < 3.5 + 1 / 3 * 1.25 + 0.01
        # Whole syllables: the loop starts and ends at zero, so it has no click.
        assert voice["first"] == 0 and abs(voice["last"]) < 0.01
        # 3-6 syllables a second, some silent: between 2 and 7.5 bursts a second.
        assert 2 * voice["seconds"] <= voice["bursts"] <= 7.5 * voice["seconds"]


def test_voices_fade_in_and_out_and_their_formants_drift_through_the_loop() -> None:
    result = _run("""
      const context = new FakeOfflineAudioContext(2, 16000, CROWD_RENDER_RATE);
      const scene = buildCrowdScene(context, context.destination, [0, 1, 2, 3, 4, 5], 60);
      console.log(JSON.stringify(scene.voices.map(voice => ({
        ramps: voice.level.gain.events.filter(event => event.kind === "ramp")
          .map(event => [event.time, event.value]),
        moves: voice.formantOne.frequency.events.filter(event => event.kind === "target")
          .map(event => [event.time, event.value]),
        movesTwo: voice.formantTwo.frequency.events.filter(event => event.kind === "target")
          .map(event => event.value),
      }))));
    """)
    for voice in result:
        levels = [value for _, value in voice["ramps"]]
        # Over a minute: several fades, alternating between silence and a speaking level.
        assert len(levels) >= 3
        assert any(value == 0 for value in levels)
        assert any(0.35 <= value <= 1 for value in levels)
        times = [time for time, _ in voice["moves"]]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        assert len(times) >= 10 and all(2 <= gap <= 5 for gap in gaps)
        assert all(300 <= value <= 800 for _, value in voice["moves"])
        assert all(900 <= value <= 2500 for value in voice["movesTwo"])


def test_the_crowd_bus_has_a_35_per_cent_stadium_reverb() -> None:
    result = _run("""
      const context = new FakeOfflineAudioContext(2, 16000, CROWD_RENDER_RATE);
      const scene = buildCrowdScene(context, context.destination, [0, 1], 20);
      console.log(JSON.stringify({
        convolvers: context.convolvers.length,
        impulseSeconds: scene.reverb.buffer.getChannelData(0).length / context.sampleRate,
        wet: scene.wet.gain.value,
        wetToOutput: scene.wet.connections.includes(context.destination),
        busToReverb: scene.bus.connections.includes(scene.reverb),
      }));
    """)
    assert result == {
        "convolvers": 1, "impulseSeconds": 2.5, "wet": pytest.approx(0.35),
        "wetToOutput": True, "busToReverb": True,
    }


def test_two_loops_of_half_the_voices_each_fold_their_end_over_their_start() -> None:
    result = _run("""
      // The rendered audio counts up, so the fold can be read back exactly.
      FakeOfflineAudioContext.fill = data => data.forEach((_, index) => { data[index] = index; });
      const loop = await settle(renderCrowdLoop(FakeOfflineAudioContext, [0, 2, 4], 17));
      const samples = loop.getChannelData(0);
      const length = 17 * CROWD_RENDER_RATE;
      const overlap = CROWD_LOOP_CROSSFADE_SECONDS * CROWD_RENDER_RATE;
      console.log(JSON.stringify({
        seconds: samples.length / CROWD_RENDER_RATE,
        start: samples[0], expectedStart: length,
        middle: samples[overlap + 5], expectedMiddle: overlap + 5,
        loops: bed.loops.map(source => ({
          seconds: source.buffer.getChannelData(0).length / CROWD_RENDER_RATE,
          loop: source.loop, toMurmur: source.connections.includes(bed.murmur),
        })),
        murmurToOutput: bed.murmur.connections.includes(bed.output),
        crowdOscillators: bed.context.oscillators.length,
      }));
    """)
    # Sample 0 is the rendered end (sample `length`); after the overlap it's the render itself.
    assert result["seconds"] == 17
    assert result["start"] == result["expectedStart"]
    assert result["middle"] == result["expectedMiddle"]
    assert result["loops"] == [
        {"seconds": 17, "loop": True, "toMurmur": True},
        {"seconds": 29, "loop": True, "toMurmur": True},
    ]
    assert result["murmurToOutput"] is True
    assert result["crowdOscillators"] == 0


def test_murmurs_swell_and_settle_only_while_the_crowd_is_running() -> None:
    result = _run("""
      advance(41000);
      const murmur = bed.murmur.gain.events.filter(event => event.kind === "ramp")
        .map(event => event.value);
      bed.pause();
      const events = bed.murmur.gain.events.length;
      advance(41000);
      advance(41000);
      console.log(JSON.stringify({
        murmur, quietWhilePaused: bed.murmur.gain.events.length === events,
        level: bed.output.gain.value,
      }));
    """)
    # Rise to a peak of 1.25-1.6, then back to 1.
    assert len(result["murmur"]) == 2
    assert 1.25 <= result["murmur"][0] <= 1.6 and result["murmur"][1] == 1
    assert result["quietWhilePaused"] is True
    assert result["level"] == 0


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
        tones: bed.context.oscillators.slice(crowdOscillators).map(node => ({
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
      console.log(JSON.stringify(bed.context.oscillators.slice(crowdOscillators).map(node => ({{
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
