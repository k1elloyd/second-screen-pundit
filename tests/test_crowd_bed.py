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
  setValueCurveAtTime(values, time, duration) {
    this.events.push({ kind: "curve", values: [...values], time, duration });
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
// The recordings' lengths, as the page's real files have them.
const FAKE_DURATIONS = { "/static/audio/crowd-bed.mp3": 66, "/static/audio/goal-roar.mp3": 14 };
class FakeAudioContext {
  constructor() {
    this.sampleRate = 100;
    // The audio clock follows the fake time while running and stands still while suspended.
    this.offset = now / 1000;
    this.frozenAt = null;
    this.state = "running";
    this.destination = new FakeNode();
    this.gains = [];
    this.oscillators = [];
    this.sources = [];
    this.compressors = [];
  }
  get currentTime() {
    return this.frozenAt !== null ? this.frozenAt : now / 1000 - this.offset;
  }
  createBuffer(_channels, length) {
    const samples = new Float32Array(length);
    return { getChannelData: () => samples };
  }
  createBufferSource() {
    const node = new FakeNode();
    this.sources.push(node);
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
  createDynamicsCompressor() {
    const node = new FakeNode();
    for (const name of ["threshold", "knee", "ratio", "attack", "release"]) {
      node[name] = new FakeParam();
    }
    this.compressors.push(node);
    return node;
  }
  decodeAudioData(data) {
    return Promise.resolve({ url: data.url, duration: FAKE_DURATIONS[data.url] });
  }
  resume() {
    if (this.frozenAt !== null) this.offset = now / 1000 - this.frozenAt;
    this.frozenAt = null;
    this.state = "running";
    return Promise.resolve();
  }
  suspend() {
    if (this.frozenAt === null) this.frozenAt = this.currentTime;
    this.state = "suspended";
    return Promise.resolve();
  }
}
class FakeOfflineAudioContext extends FakeAudioContext {}
globalThis.window = { AudioContext: FakeAudioContext, OfflineAudioContext: FakeOfflineAudioContext };
globalThis.Audio = class extends FakeNode {
  constructor() { super(); this.src = ""; }
};
globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };
const fetched = [];
globalThis.fetch = (url) => {
  fetched.push(url);
  return Promise.resolve({ ok: true, arrayBuffer: () => Promise.resolve({ url }) });
};
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
  await settle(bed.audioReady);
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


def test_the_bed_is_the_recording_looped_by_two_copies_with_an_equal_power_crossfade() -> None:
    result = _run("""
      // Play past the first loop point (66 s recording, 2 s crossfade: the next copy at 64 s).
      for (let second = 0; second < 70; second += 1) advance(1000);
      const [first, second] = bed.bedCopies;
      const curve = (copy, kind) => copy.level.gain.events.find(event => event.kind === kind
        && event.time === second.at);
      const fadeOut = curve(first, "curve");
      const fadeIn = curve(second, "curve");
      console.log(JSON.stringify({
        fetched,
        copies: bed.bedCopies.map(copy => ({ at: copy.at, stop: copy.source.stoppedAt })),
        firstStartsAtFull: first.level.gain.value,
        fadeOut: { time: fadeOut.time, duration: fadeOut.duration, values: fadeOut.values },
        fadeIn: { time: fadeIn.time, duration: fadeIn.duration, values: fadeIn.values },
        toBed: bed.bedCopies.every(copy => copy.level.connections.includes(bed.bedLevel)),
        trim: bed.bedLevel.gain.value,
        bedToMurmur: bed.bedLevel.connections.includes(bed.murmur),
        murmurToOutput: bed.murmur.connections.includes(bed.output),
        crowdOscillators: bed.context.oscillators.length,
      }));
    """)
    assert result["fetched"] == ["/static/audio/crowd-bed.mp3", "/static/audio/goal-roar.mp3"]
    # Each copy plays the whole recording; the next starts 2 s before the last one ends.
    assert result["copies"][:2] == [{"at": 0, "stop": 66}, {"at": 64, "stop": 130}]
    assert result["firstStartsAtFull"] == 1
    assert result["fadeOut"]["time"] == 64 and result["fadeOut"]["duration"] == 2
    assert result["fadeIn"]["time"] == 64 and result["fadeIn"]["duration"] == 2
    falling, rising = result["fadeOut"]["values"], result["fadeIn"]["values"]
    assert falling[0] == pytest.approx(1) and falling[-1] == pytest.approx(0, abs=1e-6)
    assert rising[0] == pytest.approx(0) and rising[-1] == pytest.approx(1)
    # Equal power: the two copies' squares add up to 1 all the way through the crossfade.
    assert all(out**2 + into**2 == pytest.approx(1, abs=1e-6) for out, into in zip(falling, rising))
    assert result["toBed"] is True
    assert result["trim"] == pytest.approx(13.2)
    assert result["bedToMurmur"] is True and result["murmurToOutput"] is True
    assert result["crowdOscillators"] == 0


def test_no_copies_pile_up_while_the_crowd_is_paused() -> None:
    result = _run("""
      bed.pause();
      const copies = bed.bedCopies.length;
      for (let second = 0; second < 200; second += 1) advance(1000);
      const whilePaused = bed.bedCopies.length;
      await bed.resumeInGesture();
      for (let second = 0; second < 70; second += 1) advance(1000);
      console.log(JSON.stringify({
        copies, whilePaused, afterResume: bed.bedCopies.map(copy => copy.at),
      }));
    """)
    assert result["copies"] == result["whilePaused"] == 1
    # The audio clock stood still while paused, so the next copy still comes 64 s in.
    assert result["afterResume"] == [0, 64]


def test_goals_play_the_roar_louder_for_my_team_and_other_moments_only_swell() -> None:
    # The roar has its own bus into the limiter, so it can duck less than the bed.
    result = _run("""
      const roars = [];
      const goal = (team) => {
        bed.lastRoar = null;
        bed.handleEvent({ type: "goal", clock: 30, team });
        roars.push(bed.lastRoar && {
          gain: bed.lastRoar.level.gain.value,
          toOutput: bed.lastRoar.level.connections.includes(bed.roarBus)
            && bed.roarBus.connections.includes(bed.limiter),
          buffer: bed.lastRoar.source.buffer.url,
        });
      };
      goal("home");
      bed.setMyTeam("home");
      goal("home");
      goal("away");
      bed.lastRoar = null;
      bed.handleEvent({ type: "card", detail: { colour: "red" } });
      bed.handleEvent({ type: "shot", xg: 0.4 });
      const otherMoments = bed.lastRoar;
      bed.setMuted(true);
      goal("home");
      bed.setMuted(false);
      bed.setEnabled(false);
      goal("home");
      console.log(JSON.stringify({ roars, otherMoments }));
    """)
    neutral, mine, theirs, muted, disabled = result["roars"]
    assert neutral == {"gain": pytest.approx(1.3), "toOutput": True,
                       "buffer": "/static/audio/goal-roar.mp3"}
    assert mine["gain"] == pytest.approx(1.3 * 1.4)
    assert theirs["gain"] == pytest.approx(1.3 * 0.6)
    assert result["otherMoments"] is None
    assert muted is None and disabled is None


def test_murmurs_lift_only_the_bed_and_only_while_the_crowd_is_running() -> None:
    result = _run("""
      advance(41000);
      const murmur = bed.murmur.gain.events.filter(event => event.kind === "ramp")
        .map(event => event.value);
      bed.handleEvent({ type: "goal", clock: 30 });
      const roarBypassesMurmur = !bed.lastRoar.level.connections.includes(bed.murmur);
      bed.pause();
      const events = bed.murmur.gain.events.length;
      advance(41000);
      advance(41000);
      console.log(JSON.stringify({
        murmur, roarBypassesMurmur,
        quietWhilePaused: bed.murmur.gain.events.length === events,
        level: bed.output.gain.value,
      }));
    """)
    # Rise to a peak of 1.25-1.6, then back to 1.
    assert len(result["murmur"]) == 2
    assert 1.25 <= result["murmur"][0] <= 1.6 and result["murmur"][1] == 1
    assert result["roarBypassesMurmur"] is True
    assert result["quietWhilePaused"] is True
    assert result["level"] == 0


def test_a_limiter_guards_the_crowd_output_and_the_whistle_goes_straight_out() -> None:
    result = _run("""
      const limiter = bed.limiter;
      console.log(JSON.stringify({
        outputToLimiter: bed.output.connections.includes(limiter),
        limiterOut: limiter.connections.includes(bed.context.destination),
        threshold: limiter.threshold.value, ratio: limiter.ratio.value, knee: limiter.knee.value,
        whistleOut: bed.effectsOutput.connections.includes(bed.context.destination),
      }));
    """)
    assert result == {
        "outputToLimiter": True, "limiterOut": True,
        "threshold": -3, "ratio": 20, "knee": 0, "whistleOut": True,
    }


def test_the_recordings_are_served_small_and_credited() -> None:
    audio = Path("src/second_screen_pundit/web/audio")
    for name in ("crowd-bed.mp3", "goal-roar.mp3"):
        assert (audio / name).stat().st_size < 1_500_000, name
    source = APP_JS.read_text(encoding="utf-8")
    assert '"/static/audio/crowd-bed.mp3"' in source and '"/static/audio/goal-roar.mp3"' in source
    credits = Path("CREDITS.md").read_text(encoding="utf-8")
    for url in (
        "https://freesound.org/people/Vogyik/sounds/868982/",
        "https://freesound.org/people/D.jones/sounds/528799/",
    ):
        assert url in credits
    assert "CC0" in credits
    assert "CREDITS.md" in Path("README.md").read_text(encoding="utf-8")


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


# A goal line's voice waits 1.8 s behind the goal roar, with the crowd on.

_GOAL_LEAD_SETUP = """
  queue.onGoalLead = (item) => bed.startGoalRoar(item.team);
  queue.onClipStart = () => bed.duckRoar();
  const plays = [];
  const play = queue.el.play.bind(queue.el);
  queue.el.play = () => { plays.push({ src: queue.el.src, at: now }); return play(); };
  const roars = () => bed.context.sources.filter((s) => s.buffer?.url === "/static/audio/goal-roar.mp3");
  const step = (ms) => { for (let t = 0; t < ms; t += 50) advance(50); };
"""


def _goal_lead(actions: str) -> dict[str, object]:
    return _run(_GOAL_LEAD_SETUP + actions)


@pytest.mark.parametrize("tier", ["goal", "goal_equaliser", "goal_peak"])
def test_a_goal_line_starts_the_roar_and_waits_1_8_s_before_the_voice(tier: str) -> None:
    result = _goal_lead(f"""
      const enqueuedAt = now;
      queue.enqueue({{ id: "goal-line", kind: "line", lineId: "goal-line", tier: "{tier}",
                       team: "home" }});
      const roarAtOnce = roars().length;
      step(3000);
      console.log(JSON.stringify({{ roarAtOnce, lead: plays[0].at - enqueuedAt,
                                    roarStartedAt: roars()[0].startedAt }}));
    """)
    assert result["roarAtOnce"] == 1
    assert result["lead"] == 1800


@pytest.mark.parametrize(
    ("setup", "tier", "kind"),
    [
        ("", "tactical", "line"),
        ("", "red_card", "line"),
        ("", "goal", "answer"),
        ("", "goal", "recap"),
        ("bed.setEnabled(false);", "goal", "line"),
        ("bed.setMuted(true);", "goal", "line"),
    ],
    ids=["tactical", "red-card", "answer", "recap", "crowd-off", "crowd-muted"],
)
def test_no_lead_for_other_clips_or_with_the_crowd_off_or_muted(setup: str, tier: str, kind: str) -> None:
    result = _goal_lead(f"""
      {setup}
      const enqueuedAt = now;
      queue.enqueue({{ id: "clip", kind: "{kind}", lineId: "clip", tier: "{tier}" }});
      step(3000);
      console.log(JSON.stringify({{ lead: plays[0].at - enqueuedAt, roars: roars().length }}));
    """)
    # The usual 300 ms pre-duck only, and no roar started for the clip.
    assert result == {"lead": 300, "roars": 0}


def test_a_muted_queue_plays_nothing_and_starts_no_roar() -> None:
    result = _goal_lead("""
      bed.setMuted(true);
      queue.setMuted(true);
      queue.enqueue({ id: "goal-line", kind: "line", lineId: "goal-line", tier: "goal" });
      step(3000);
      console.log(JSON.stringify({ plays: plays.length, roars: roars().length }));
    """)
    assert result == {"plays": 0, "roars": 0}


def test_the_roar_ducks_to_half_when_the_voice_starts_and_the_bed_ducks_as_before() -> None:
    result = _goal_lead("""
      bed.handleEvent({ type: "goal", clock: 30, team: "home" });
      const swell = bed.output.gain.value;
      step(500);
      queue.enqueue({ id: "goal-line", kind: "line", lineId: "goal-line", tier: "goal" });
      step(1000);
      const duringLead = { roar: bed.roarBus.gain.value, roars: roars().length };
      step(800);
      const duringVoice = { roar: bed.roarBus.gain.value, bed: bed.output.gain.value };
      console.log(JSON.stringify({ swell, duringLead, duringVoice, plays: plays.length }));
    """)
    swell = result["swell"]
    assert swell == pytest.approx(0.09)
    # The goal event's roar is reused, at the crowd's full level through the lead.
    assert result["duringLead"] == {"roar": pytest.approx(swell), "roars": 1}
    assert result["plays"] == 1
    # Half the roar under the voice; the bed takes its usual duck.
    assert result["duringVoice"]["roar"] == pytest.approx(swell * 0.5)
    assert result["duringVoice"]["bed"] == pytest.approx(0.01)


def test_the_clip_behind_a_goal_line_plays_next_in_order() -> None:
    result = _goal_lead("""
      queue.enqueue({ id: "goal-line", kind: "line", lineId: "goal-line", tier: "goal",
                      expiresAt: now + LINE_AUDIO_STALE_MS });
      step(100);
      queue.enqueue({ id: "next-line", kind: "line", lineId: "next-line", tier: "tactical",
                      expiresAt: now + LINE_AUDIO_STALE_MS });
      step(1800);
      const first = plays.map((p) => p.src);
      step(2000);
      queue.el.handlers.ended();
      const endedAt = now;
      step(1000);
      console.log(JSON.stringify({ first, order: plays.map((p) => p.src),
                                   nextAfter: plays[1].at - endedAt }));
    """)
    assert result["first"] == ["goal-line"]
    assert result["order"] == ["goal-line", "next-line"]
    assert result["nextAfter"] == 300
