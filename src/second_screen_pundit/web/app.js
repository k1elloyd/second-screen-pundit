const el = (id) => document.getElementById(id);
// This viewer's ID, kept for the tab's life: one replay per viewer, not per network address.
const clientId = sessionStorage.getItem("pundit-client-id") || crypto.randomUUID();
sessionStorage.setItem("pundit-client-id", clientId);

// Mode and My team: kept per viewer for the tab's life and sent with Start. A change while a
// match runs goes to the replay and applies from the next line.
const PROFILE_KEY = "pundit-profile";
const PROFILE_MODES = ["casual", "analyst"];
const PROFILE_TEAMS = ["neutral", "home", "away"];

function loadProfile() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(PROFILE_KEY) || "{}");
    return {
      mode: PROFILE_MODES.includes(saved.mode) ? saved.mode : "casual",
      team: PROFILE_TEAMS.includes(saved.team) ? saved.team : "neutral",
    };
  } catch {
    return { mode: "casual", team: "neutral" };
  }
}

const KEY_EVENT_TYPES = ["goal", "card", "substitution", "formation_change", "half_time", "full_time"];
// The feed auto-scrolls only when the viewer is this close to the newest line.
const STICK_TO_BOTTOM_PX = 80;
const MAX_FEED_CARDS = 200;

const state = {
  warmup: "warming",
  replayId: null,
  // From /start; held in memory only and sent with every call about this replay.
  replayToken: null,
  // False when the server started this replay with audio off (the daily spending cap).
  audio: true,
  controller: null,
  // The replay's stream position, kept across connections so a reconnect resumes after the
  // last message handled.
  stream: null,
  // The viewer paused (the toggle), or the page paused while hidden and is waiting for the
  // Welcome back tap. Either way a reconnect must not resume the match.
  userPaused: false,
  awayPaused: false,
  match: null,
  replayComplete: false,
  score: { home: 0, away: 0 },
  events: 0,
  eventsById: new Map(),
  stats: null,
  recording: null,
  // question_id -> the answer card waiting for a spoken question's answer over SSE.
  pendingAnswers: new Map(),
  questionAudioSeconds: 15,
  questionAudioBytes: 1_000_000,
  questionLimit: 300,
  profile: loadProfile(),
};

function freshStats() {
  const side = () => ({ shots: 0, target: 0, fouls: 0, cards: 0, formation: "–" });
  // Whole-number shares from the server's possession_pair, so the two always add up to 100.
  return { home: side(), away: side(), possession: null };
}
state.stats = freshStats();

function showMessage(target, text, isError = false) {
  target.textContent = text || "";
  target.classList.toggle("error", isError);
}

// "idle" before a match, "running" while one plays, "done" after full time. Under 1024px it
// picks which controls show and how much the header carries.
function setPhase(phase) {
  el("app-shell").dataset.phase = phase;
}

// Every Mode and My team select (the control bar's, and Match info's on phones) shows the
// same profile; the shell carries it for styling.
function renderProfile() {
  for (const select of document.querySelectorAll('[data-profile="mode"]')) select.value = state.profile.mode;
  for (const select of document.querySelectorAll('[data-profile="team"]')) select.value = state.profile.team;
  el("app-shell").dataset.mode = state.profile.mode;
  el("app-shell").dataset.team = state.profile.team;
}

// "My team: Ashbridge" on wide screens; "♥ Ashbridge" below 1440px, so the control bar stays
// one row at 1280px and Mode, My team and Start share one row on phones (the select's label
// still says "My team" to screen readers).
const wideTeamLabels = matchMedia("(min-width: 1440px)");
const teamLabels = { home: "Home", away: "Away" };
function setTeamOptionLabels(homeShort = teamLabels.home, awayShort = teamLabels.away) {
  teamLabels.home = homeShort;
  teamLabels.away = awayShort;
  const wide = wideTeamLabels.matches;
  for (const option of document.querySelectorAll('[data-profile="team"] option')) {
    const side = option.dataset.side;
    const name = side ? teamLabels[side] : "Neutral";
    option.textContent = wide ? `My team: ${name}` : side ? `♥ ${name}` : name;
  }
}
wideTeamLabels.addEventListener("change", () => setTeamOptionLabels());

async function changeProfile(change) {
  state.profile = { ...state.profile, ...change };
  try {
    sessionStorage.setItem(PROFILE_KEY, JSON.stringify(state.profile));
  } catch {
    // Storage blocked: the choice still holds for this page and is sent with Start.
  }
  renderProfile();
  if (!state.replayId || !state.replayToken) return;
  try {
    await responseJson(await fetch(`/api/replays/${state.replayId}/profile`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify(state.profile),
    }));
    if (!state.replayComplete) {
      const mode = state.profile.mode === "analyst" ? "Analyst" : "Casual";
      showMessage(el("ask-message"), `${mode} view from the next line.`);
    }
  } catch {
    showMessage(el("ask-message"), "Your view will apply from your next match.");
  }
}

// One connection state for the scoreboard's dot, the desktop footer and the Match info panel.
// kind: "idle" (grey), "connected" (green), "reconnecting" (gold) or "lost" (red).
function setConnection(text, kind = "connected") {
  el("connection-state").textContent = text;
  el("connection-detail").textContent = text;
  const dot = el("live-light");
  dot.classList.toggle("connected", kind === "connected");
  dot.classList.toggle("reconnecting", kind === "reconnecting");
  dot.classList.toggle("lost", kind === "lost");
  dot.setAttribute("aria-label", text);
  dot.title = text;
}

// Pause and Resume are one button; data-paused picks its icon and label.
function setPauseButton({ paused = false, enabled = true } = {}) {
  const button = el("pause-button");
  button.disabled = !enabled;
  button.dataset.paused = String(paused);
  el("pause-label").textContent = paused ? "Resume" : "Pause";
  button.title = paused ? "Resume" : "Pause";
}

async function responseJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail
      : typeof detail?.message === "string" ? detail.message
      : `Request failed (${response.status}).`;
    const error = new Error(message);
    error.status = response.status;
    error.code = detail?.code || null;
    throw error;
  }
  return body;
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadMatches() {
  const matches = await responseJson(await fetch("/api/matches"));
  const picker = el("match-picker");
  picker.replaceChildren();
  for (const match of matches) {
    const option = document.createElement("option");
    option.value = match.id;
    option.textContent = match.label;
    option.dataset.home = match.home;
    option.dataset.away = match.away;
    option.dataset.homeShort = match.home_short || match.home;
    option.dataset.awayShort = match.away_short || match.away;
    option.dataset.stadium = match.stadium;
    picker.append(option);
  }
  updateMatchLabels();
}

function updateMatchLabels() {
  const selected = [...el("match-picker").options].find((item) => item.selected);
  if (!selected) return;
  const home = selected.dataset.home || "Home";
  const away = selected.dataset.away || "Away";
  for (const id of ["home-name", "stats-home", "legend-home"]) el(id).textContent = home;
  for (const id of ["away-name", "stats-away", "legend-away"]) el(id).textContent = away;
  // The phone header shows the short forms (engine/teams.py), the same ones the lines use.
  el("home-short").textContent = selected.dataset.homeShort || home;
  el("away-short").textContent = selected.dataset.awayShort || away;
  setTeamOptionLabels(selected.dataset.homeShort || home, selected.dataset.awayShort || away);
  el("stadium").textContent = selected.dataset.stadium || "";
}

const WARMUP_POLL_MS = 2500;
const warmupStarted = Date.now();
const FEED_WAITING_TEXT = "Commentary and audio will arrive with the match. The viewer feed runs on a short broadcast delay.";
let fixturesLoaded = false;

function warmingText() {
  return `Warming up the pundit… ${Math.round((Date.now() - warmupStarted) / 1000)}s`;
}

// Before a match, the feed's empty state says what the pundit is doing: it's the one message
// that shows on every screen size (the status chip hides its text on phones).
function setFeedWaiting(text) {
  const empty = el("feed-empty");
  if (empty) empty.textContent = text;
}

function showWarmStatus(kind, text) {
  const status = el("warm-status");
  status.classList.toggle("ready", kind === "ready");
  status.classList.toggle("failed", kind === "failed");
  status.lastElementChild.textContent = text;
  status.title = text;
}

// Polls /healthz until the pundit is ready, then stops, so an open tab doesn't keep the app
// awake. While the container is starting, /healthz fails (502/503 or no connection): that is
// still "warming up", never an error.
async function checkWarmup() {
  let health = null;
  try {
    if (!fixturesLoaded) {
      await loadMatches();
      fixturesLoaded = true;
    }
    const response = await fetch("/healthz", { cache: "no-store" });
    if (response.ok) health = await response.json();
  } catch {
    health = null;
  }
  if (!health) {
    showWarmStatus("warming", warmingText());
    setFeedWaiting(warmingText());
    setTimeout(checkWarmup, WARMUP_POLL_MS);
    return;
  }
  state.warmup = health.warmup;
  const limits = health.limits || {};
  state.questionLimit = limits.question_characters || 300;
  state.questionAudioSeconds = limits.question_audio_seconds || 15;
  state.questionAudioBytes = limits.question_audio_bytes || 1_000_000;
  el("question-input").maxLength = state.questionLimit;
  updateCharacterCount();
  document.querySelector(".limit-note").textContent =
    `${limits.questions_per_minute || 6} questions / ${limits.question_rate_window_seconds || 60}s`;
  if (health.ready) {
    showWarmStatus("ready", health.limited ? "Pundit ready · cached" : "Pundit ready");
    setFeedWaiting(FEED_WAITING_TEXT);
  } else if (health.warmup === "failed") {
    showWarmStatus("failed", "Pundit unavailable");
    setFeedWaiting("The pundit couldn't start. Please try again in a few minutes.");
  } else {
    showWarmStatus("warming", warmingText());
    setFeedWaiting(warmingText());
  }
  const busy = Boolean(state.replayId) && !state.replayComplete;
  el("match-picker").disabled = !health.ready || busy;
  el("start-button").disabled = !health.ready || busy;
  // Keep polling only until ready. While a failed warm-up is retried on the server, Start
  // already works (cached mode), and a slow poll shows when live commentary is back.
  if (!health.ready || health.warmup === "failed") {
    setTimeout(checkWarmup, health.warmup === "failed" ? 15000 : WARMUP_POLL_MS);
  }
}

function eventText(event) {
  const team = event.team_name ? `${event.team_name}: ` : "";
  const player = event.player_name ? `${event.player_name} ` : "";
  const labels = {
    kickoff: "Kick-off",
    pass: event.outcome === "success" ? "completed a pass" : "misplaced a pass",
    carry: event.outcome === "success" ? "carried the ball forward" : "lost the ball in a carry",
    shot: event.outcome === "success" ? "had a shot on target" : "had a shot",
    goal: "GOAL",
    save: "made a save",
    tackle: event.outcome === "success" ? "won a tackle" : "missed a tackle",
    interception: event.outcome === "success" ? "made an interception" : "missed an interception",
    foul: "committed a foul",
    card: `${event.detail.colour || ""} card`,
    substitution: "made a substitution",
    formation_change: `changed shape to ${event.detail.formation || "a new formation"}`,
    half_time: "Half-time",
    full_time: "Full-time",
  };
  return `${team}${player}${labels[event.type] || event.type}`;
}

// Match info: formations, stats, key moments and the pitch mini-map, all from released events.

function renderStats() {
  for (const side of ["home", "away"]) {
    const stats = state.stats[side];
    el(`formation-${side}`).textContent = stats.formation;
    el(`shots-${side}`).textContent = String(stats.shots);
    el(`target-${side}`).textContent = String(stats.target);
    el(`fouls-${side}`).textContent = String(stats.fouls);
    el(`cards-${side}`).textContent = String(stats.cards);
    const possession = state.stats.possession;
    el(`possession-${side}`).textContent = possession ? `${possession[side]}%` : "–";
  }
}

function countEvent(event) {
  if (event.possession) state.stats.possession = event.possession;
  const stats = event.team ? state.stats[event.team] : null;
  if (!stats) return;
  if (event.type === "shot") {
    stats.shots += 1;
    // For a shot, outcome "success" means on target (event schema).
    if (event.outcome === "success") stats.target += 1;
  }
  if (event.type === "foul") stats.fouls += 1;
  if (event.type === "card") stats.cards += 1;
  if ((event.type === "kickoff" && event.period === 1) || event.type === "formation_change") {
    if (event.detail.formation) stats.formation = event.detail.formation;
  }
}

function addKeyMoment(event) {
  if (!KEY_EVENT_TYPES.includes(event.type)) return;
  const list = el("key-moments");
  list.querySelector(".empty-state")?.remove();
  const item = make("li", [event.team || "", event.type].join(" ").trim());
  const clock = make("span", "moment-clock",
    event.type === "half_time" ? "HT" : event.type === "full_time" ? "FT" : `${event.clock}'`);
  item.append(clock, make("span", "moment-text", eventText(event)));
  list.prepend(item);
}

// SVG elements have no `hidden` property, so visibility goes through `display`.
function showSvg(node, visible) {
  node.style.display = visible ? "" : "none";
}

// Coordinates are normalised so the acting team attacks towards x = 100; the away team's
// are flipped for display so both teams share one pitch (home attacks left to right).
function pitchPoint(x, y, team) {
  const flip = team === "away";
  return [(flip ? 100 - x : x) * 1.05, (flip ? 100 - y : y) * 0.68];
}

function renderPitch(event) {
  if (event.x == null || event.y == null) return;
  const team = event.team === "away" ? "away" : "home";
  const [cx, cy] = pitchPoint(event.x, event.y, team);
  const ball = el("pitch-ball");
  ball.setAttribute("cx", cx.toFixed(2));
  ball.setAttribute("cy", cy.toFixed(2));
  ball.setAttribute("class", `pitch-ball ${team}`);
  showSvg(ball, true);
  const trail = el("pitch-trail");
  if (event.end_x != null && event.end_y != null) {
    const [ex, ey] = pitchPoint(event.end_x, event.end_y, team);
    trail.setAttribute("x1", cx.toFixed(2));
    trail.setAttribute("y1", cy.toFixed(2));
    trail.setAttribute("x2", ex.toFixed(2));
    trail.setAttribute("y2", ey.toFixed(2));
    trail.setAttribute("class", `pitch-trail ${team}`);
    showSvg(trail, true);
  } else {
    showSvg(trail, false);
  }
  el("pitch-caption").textContent = `${event.clock}' · ${eventText(event)}`;
}

function renderEvent(event) {
  state.eventsById.set(event.event_id, event);
  if (event.type === "goal" && event.team) state.score[event.team] += 1;
  el("home-score").textContent = String(state.score.home);
  el("away-score").textContent = String(state.score.away);
  el("clock").textContent = event.type === "full_time" ? "FULL TIME" :
    event.type === "half_time" ? "HALF-TIME" : `${event.clock}'`;
  if (event.type === "full_time") el("live-tag").textContent = "FULL TIME";
  else if (event.type !== "half_time") {
    el("live-tag").textContent = "ON AIR";
    el("live-tag").classList.add("on-air");
  }
  state.events += 1;
  countEvent(event);
  renderStats();
  addKeyMoment(event);
  renderPitch(event);
  if (event.type === "full_time") setConnection("Full time · recap pending");
}

// The feed: commentary, questions, answers and the recap, newest at the bottom.

// Whether the viewer is following the newest line; kept across resizes and the keyboard.
let feedFollowing = true;

function feedIsNearBottom() {
  const feed = el("commentary");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= STICK_TO_BOTTOM_PX;
}

function scrollFeedToBottom() {
  const feed = el("commentary");
  feed.scrollTop = feed.scrollHeight;
  feedFollowing = true;
  el("new-lines").hidden = true;
}

// Append (or swap in) a card, keeping the viewer at the newest line only if they were there.
function placeInFeed(card, { replace = null, forceScroll = false } = {}) {
  const feed = el("commentary");
  const stick = forceScroll || feedIsNearBottom();
  el("feed-empty")?.remove();
  if (replace && replace.isConnected) replace.replaceWith(card);
  else feed.append(card);
  while (feed.children.length > MAX_FEED_CARDS) {
    const oldest = feed.firstElementChild;
    const height = oldest.offsetHeight;
    oldest.remove();
    if (!stick) feed.scrollTop -= height;
  }
  if (stick) scrollFeedToBottom();
  else if (!replace) el("new-lines").hidden = false;
}

// A card that grows (a Replay button arriving) keeps the newest line in view if it was.
function keepFeedPinned(change) {
  const stick = feedIsNearBottom();
  change();
  if (stick) scrollFeedToBottom();
}

function audioUrl(id) {
  return id ? `/api/audio/${encodeURIComponent(id)}` : "";
}

// A line's audio is dropped if it hasn't started this long after the line arrived, so the
// voice never falls far behind the timeline.
const LINE_AUDIO_STALE_MS = 12_000;

function readStored(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function writeStored(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage unavailable: setting not kept */ }
}

// A 0.1 s silent WAV, built in the click handler so play() runs inside the user gesture.
function silentAudioUrl() {
  return URL.createObjectURL(encodeWav(new Float32Array(1600), 16000));
}

const CROWD_BASE_GAIN = 0.04;
const CROWD_DUCK_FACTOR = 0.25;
const CROWD_SWELL_MS = 3000;
const CROWD_DUCK_RELEASE_MS = 400;
const CROWD_DUCK_FADE_SECONDS = 0.08;
const CROWD_LEVEL_FADE_SECONDS = 0.14;
const CROWD_LAYER_CUTOFFS = [400, 470, 540, 610];
const CROWD_LAYER_PANS = [-0.6, -0.2, 0.2, 0.6];
const CROWD_LAYER_GAIN = 0.38;
const CROWD_SURGE_HZ = 0.012;
const CROWD_SURGE_DEPTH = 0.1;
const WHISTLE_SHORT_SECONDS = 0.16;
const WHISTLE_LONG_SECONDS = 0.42;
const WHISTLE_GAP_SECONDS = 0.12;
const WHISTLE_PEAK_GAIN = 0.16;
const WHISTLE_FREQUENCIES = [1850, 2250];

class CrowdBed {
  constructor() {
    this.enabled = readStored("pundit-crowd-enabled") !== "0";
    this.muted = readStored("pundit-muted") === "1";
    this.ducked = false;
    this.active = false;
    this.context = null;
    this.output = null;
    this.effectsOutput = null;
    this.swellLevel = 0;
    this.swellUntil = 0;
    this.swellTimer = null;
    this.duckReleaseTimer = null;
    this.whistles = new Set();
    this.whistledKickoffs = new Set();
  }

  create() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("Web Audio is not supported in this browser.");
    this.context = new AudioContextClass();
    const sampleRate = this.context.sampleRate;
    const output = this.context.createGain();
    output.gain.value = 0;
    const effectsOutput = this.context.createGain();
    effectsOutput.gain.value = 0.65;
    const surge = this.context.createGain();
    surge.gain.value = 1;
    surge.connect(output);
    output.connect(this.context.destination);
    effectsOutput.connect(this.context.destination);

    for (let layer = 0; layer < CROWD_LAYER_CUTOFFS.length; layer += 1) {
      const buffer = this.context.createBuffer(1, sampleRate * 2, sampleRate);
      const samples = buffer.getChannelData(0);
      let lowFrequencyNoise = 0;
      const smoothing = 0.032 + Math.random() * 0.016;
      for (let index = 0; index < samples.length; index += 1) {
        const white = Math.random() * 2 - 1;
        lowFrequencyNoise += (white - lowFrequencyNoise) * smoothing;
        samples[index] = Math.max(-1, Math.min(1, lowFrequencyNoise * 4));
      }

      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.loop = true;
      const highPass = this.context.createBiquadFilter();
      highPass.type = "highpass";
      highPass.frequency.value = 85 + layer * 8;
      const lowPass = this.context.createBiquadFilter();
      lowPass.type = "lowpass";
      lowPass.frequency.value = CROWD_LAYER_CUTOFFS[layer];
      const layerGain = this.context.createGain();
      layerGain.gain.value = CROWD_LAYER_GAIN;
      const panner = this.context.createStereoPanner();
      panner.pan.value = CROWD_LAYER_PANS[layer];
      source.connect(highPass);
      highPass.connect(lowPass);
      lowPass.connect(layerGain);
      layerGain.connect(panner);
      panner.connect(surge);

      const lfo = this.context.createOscillator();
      lfo.frequency.value = 0.018 + Math.random() * 0.02;
      const levelModulation = this.context.createGain();
      levelModulation.gain.value = 0.1;
      const filterModulation = this.context.createGain();
      filterModulation.gain.value = 28 + Math.random() * 20;
      lfo.connect(levelModulation);
      levelModulation.connect(layerGain.gain);
      lfo.connect(filterModulation);
      filterModulation.connect(lowPass.frequency);
      source.start();
      lfo.start(0, Math.random() * 20);
    }

    const surgeLfo = this.context.createOscillator();
    surgeLfo.frequency.value = CROWD_SURGE_HZ;
    const surgeModulation = this.context.createGain();
    surgeModulation.gain.value = CROWD_SURGE_DEPTH;
    surgeLfo.connect(surgeModulation);
    surgeModulation.connect(surge.gain);
    surgeLfo.start();
    this.output = output;
    this.effectsOutput = effectsOutput;
  }

  startInGesture() {
    if (this.muted) return Promise.resolve();
    if (!this.context) this.create();
    this.active = true;
    this.applyLevel();
    if (this.context.state === "suspended") return this.context.resume();
    return Promise.resolve();
  }

  resumeInGesture() {
    return this.startInGesture();
  }

  applyLevel() {
    if (!this.context || !this.output) return;
    const now = Date.now();
    let level = 0;
    if (this.active && this.enabled && !this.muted) {
      level = this.ducked
        ? CROWD_BASE_GAIN * CROWD_DUCK_FACTOR
        : this.swellUntil > now ? Math.max(CROWD_BASE_GAIN, this.swellLevel) : CROWD_BASE_GAIN;
    }
    const parameter = this.output.gain;
    parameter.cancelScheduledValues(this.context.currentTime);
    parameter.setTargetAtTime(
      level,
      this.context.currentTime,
      this.ducked ? CROWD_DUCK_FADE_SECONDS : CROWD_LEVEL_FADE_SECONDS,
    );
  }

  setDucked(ducked) {
    clearTimeout(this.duckReleaseTimer);
    this.duckReleaseTimer = null;
    if (ducked) {
      this.ducked = true;
      this.applyLevel();
      return;
    }
    this.duckReleaseTimer = setTimeout(() => {
      this.ducked = false;
      this.duckReleaseTimer = null;
      this.applyLevel();
    }, CROWD_DUCK_RELEASE_MS);
  }

  setMuted(muted) {
    this.muted = muted;
    if (muted) this.clearWhistles();
    this.applyLevel();
  }

  setEnabled(enabled) {
    this.enabled = enabled;
    writeStored("pundit-crowd-enabled", enabled ? "1" : "0");
    if (!enabled) this.clearWhistles();
    this.applyLevel();
  }

  swell(level) {
    if (!this.active || !this.enabled || this.muted) return;
    this.swellLevel = Math.max(this.swellLevel, level);
    this.swellUntil = Date.now() + CROWD_SWELL_MS;
    clearTimeout(this.swellTimer);
    this.swellTimer = setTimeout(() => {
      this.swellLevel = 0;
      this.swellUntil = 0;
      this.swellTimer = null;
      this.applyLevel();
    }, CROWD_SWELL_MS);
    this.applyLevel();
  }

  handleEvent(event) {
    if (event.type === "kickoff" && !this.whistledKickoffs.has(event.period)) {
      this.whistledKickoffs.add(event.period);
      this.playWhistlePattern([WHISTLE_SHORT_SECONDS]);
    } else if (event.type === "half_time") {
      this.playWhistlePattern([WHISTLE_SHORT_SECONDS, WHISTLE_SHORT_SECONDS]);
    } else if (event.type === "full_time") {
      this.playWhistlePattern([
        WHISTLE_SHORT_SECONDS, WHISTLE_SHORT_SECONDS, WHISTLE_LONG_SECONDS,
      ]);
    }

    if (event.type === "goal") {
      this.swell((event.clock || 0) >= 75 ? 0.14 : 0.09);
    } else if (event.type === "card" && event.detail?.colour === "red") {
      this.swell(0.06);
    } else if (event.type === "shot" && event.xg != null && event.xg >= 0.3) {
      this.swell(0.06);
    }
  }

  playWhistlePattern(durations) {
    if (!this.active || !this.enabled || this.muted || !this.context || !this.effectsOutput) return;
    let startAt = this.context.currentTime;
    for (const duration of durations) {
      const envelope = this.context.createGain();
      envelope.gain.setValueAtTime(0, startAt);
      envelope.gain.linearRampToValueAtTime(WHISTLE_PEAK_GAIN, startAt + 0.025);
      envelope.gain.setValueAtTime(WHISTLE_PEAK_GAIN, startAt + duration - 0.045);
      envelope.gain.linearRampToValueAtTime(0, startAt + duration);
      envelope.connect(this.effectsOutput);
      this.whistles.add(envelope);

      for (const frequency of WHISTLE_FREQUENCIES) {
        const oscillator = this.context.createOscillator();
        oscillator.type = "sine";
        oscillator.frequency.value = frequency;
        oscillator.connect(envelope);
        oscillator.start(startAt);
        oscillator.stop(startAt + duration);
      }
      startAt += duration + WHISTLE_GAP_SECONDS;
    }
  }

  clearWhistles() {
    if (this.context) {
      for (const envelope of this.whistles) {
        envelope.gain.cancelScheduledValues(this.context.currentTime);
        envelope.gain.setValueAtTime(0, this.context.currentTime);
      }
    }
    this.whistles.clear();
  }

  resetEventCues() {
    this.clearWhistles();
    this.whistledKickoffs.clear();
  }

  pause() {
    this.active = false;
    clearTimeout(this.duckReleaseTimer);
    this.duckReleaseTimer = null;
    this.ducked = false;
    this.clearWhistles();
    clearTimeout(this.swellTimer);
    this.swellTimer = null;
    this.swellLevel = 0;
    this.swellUntil = 0;
    if (!this.context || !this.output) return;
    const parameter = this.output.gain;
    parameter.cancelScheduledValues(this.context.currentTime);
    parameter.setValueAtTime(0, this.context.currentTime);
    if (this.context.state !== "suspended") {
      this.context.suspend().catch((error) => console.error("Could not suspend crowd audio.", error));
    }
  }
}

// One <audio> element plays everything in turn, so lines never overlap. Lines wait in order;
// normally at most one waits behind the line that is playing. Answers, the recap and replays
// go to the front without cutting off the line that is playing.
// A goal, red card or late-drama clip is never dropped and never goes stale: in a burst the
// oldest routine clip is dropped first, and a stale routine clip is skipped.
const PRIORITY_AUDIO_TIERS = new Set(["goal_peak", "goal_equaliser", "goal", "red_card", "big_chance"]);
const MAX_WAITING_LINES = 1;
const AUDIO_PRE_DUCK_MS = 300;

class AudioQueue {
  constructor() {
    this.el = new Audio();
    this.items = [];
    this.playing = null;
    this.paused = false;
    // While paused the match is paused too, so waiting clips don't go stale meanwhile.
    this.pausedAt = null;
    this.pausedTotal = 0;
    this.muted = readStored("pundit-muted") === "1";
    this.onPlaybackChange = () => {};
    this.onLineQueueChange = () => {};
    this.pendingLines = new Set();
    this.lineQueueBusy = false;
    this.playTimer = null;
    this.playbackStarted = false;
    this.el.addEventListener("ended", () => this.next());
    this.el.addEventListener("error", () => this.next());
  }

  // Call synchronously inside the Start click handler, before any await.
  unlock() {
    const url = silentAudioUrl();
    this.el.src = url;
    return this.el.play()
      .then(() => {
        if (this.el.src === url) this.el.pause();
        el("enable-audio").hidden = true;
      })
      .catch(() => {})
      .finally(() => URL.revokeObjectURL(url));
  }

  enqueue({ id, kind, lineId = null, expiresAt = null, tier = null }) {
    if (kind === "line" && lineId) this.pendingLines.delete(lineId);
    if (!id || this.muted) {
      this.notifyLineQueueChange();
      return;
    }
    const priority = kind === "line" && PRIORITY_AUDIO_TIERS.has(tier);
    const item = { url: audioUrl(id), kind, lineId, expiresAt, priority };
    if (kind === "line") {
      this.items.push(item);
      this.trimLines();
    } else {
      this.items.unshift(item);
    }
    this.notifyLineQueueChange();
    if (!this.playing && !this.paused) this.next();
  }

  setLinePending(lineId, pending) {
    if (!lineId) return;
    if (pending && !this.muted) this.pendingLines.add(lineId);
    else this.pendingLines.delete(lineId);
    this.notifyLineQueueChange();
  }

  notifyLineQueueChange(force = false) {
    const busy = Boolean(this.playing || this.items.length || this.pendingLines.size);
    if (force || busy !== this.lineQueueBusy) {
      this.lineQueueBusy = busy;
      this.onLineQueueChange(busy);
    }
  }

  static stale(item) {
    return !item.priority && item.expiresAt != null && item.expiresAt < Date.now();
  }

  // Stale routine clips go; then, while too many lines wait, the oldest routine clip goes.
  trimLines() {
    this.items = this.items.filter((queued) => !AudioQueue.stale(queued));
    const waitingLines = () => this.items.filter((queued) => queued.kind === "line");
    while (waitingLines().length > MAX_WAITING_LINES) {
      const oldestRoutine = waitingLines().find((queued) => !queued.priority);
      if (!oldestRoutine) break;
      this.items.splice(this.items.indexOf(oldestRoutine), 1);
    }
  }

  next() {
    clearTimeout(this.playTimer);
    this.playTimer = null;
    this.playbackStarted = false;
    this.playing = null;
    document.querySelector(".commentary-card.speaking")?.classList.remove("speaking");
    if (this.paused) {
      this.onPlaybackChange(false);
      this.notifyLineQueueChange();
      return;
    }
    const item = this.items.shift();
    if (!item) {
      this.onPlaybackChange(false);
      this.notifyLineQueueChange();
      return;
    }
    if (this.muted || AudioQueue.stale(item)) {
      this.next();
      return;
    }
    this.playing = item;
    this.notifyLineQueueChange();
    this.schedulePlay(item);
  }

  schedulePlay(item) {
    this.onPlaybackChange(true);
    this.playTimer = setTimeout(() => {
      this.playTimer = null;
      if (this.paused || this.playing !== item) return;
      if (this.muted || AudioQueue.stale(item)) {
        this.next();
        return;
      }
      this.playbackStarted = true;
      this.el.src = item.url;
      if (item.lineId) {
        [...document.querySelectorAll(".commentary-card")]
          .find((card) => card.dataset.lineId === item.lineId)?.classList.add("speaking");
      }
      this.el.play().catch((error) => {
        if (error.name === "NotAllowedError") {
          this.items.unshift(item);
          this.playing = null;
          this.playbackStarted = false;
          this.onPlaybackChange(false);
          el("enable-audio").hidden = false;
        } else {
          this.next();
        }
      });
    }, AUDIO_PRE_DUCK_MS);
  }

  setMuted(muted) {
    this.muted = muted;
    writeStored("pundit-muted", muted ? "1" : "0");
    if (muted) {
      this.el.pause();
      this.items = [];
      this.pendingLines.clear();
      this.next();
    }
  }

  // Milliseconds spent paused so far, the current pause included.
  pausedMs() {
    return this.pausedTotal + (this.pausedAt === null ? 0 : Date.now() - this.pausedAt);
  }

  pause() {
    this.paused = true;
    if (this.pausedAt === null) this.pausedAt = Date.now();
    clearTimeout(this.playTimer);
    this.playTimer = null;
    this.el.pause();
    this.onPlaybackChange(false);
  }

  resume() {
    if (this.pausedAt !== null) {
      const away = Date.now() - this.pausedAt;
      this.pausedTotal += away;
      this.pausedAt = null;
      for (const item of this.items) if (item.expiresAt != null) item.expiresAt += away;
    }
    this.paused = false;
    if (this.playing) this.schedulePlay(this.playing);
    else this.next();
  }

  // Resume from a tap. iOS needs a play() inside the gesture after the screen was locked:
  // the clip that was playing, the next one, or (with nothing queued) a silent unlock.
  resumeInGesture() {
    if (this.playing || this.items.length) {
      this.resume();
      return;
    }
    this.unlock();
    this.resume();
  }

  clear() {
    this.items = [];
    this.pendingLines.clear();
    clearTimeout(this.playTimer);
    this.playTimer = null;
    this.playbackStarted = false;
    this.el.pause();
    this.playing = null;
    this.onPlaybackChange(false);
    this.notifyLineQueueChange();
    document.querySelector(".commentary-card.speaking")?.classList.remove("speaking");
  }
}

const audioQueue = new AudioQueue();
const crowdBed = new CrowdBed();
audioQueue.onPlaybackChange = (speaking) => crowdBed.setDucked(speaking);
let playbackStateRevision = 0;
let playbackStateRequests = Promise.resolve();

function reportViewerPlaybackState(audioBusy) {
  if (!state.replayId || !state.replayToken) return;
  const replayId = state.replayId;
  const token = state.replayToken;
  const revision = ++playbackStateRevision;
  playbackStateRequests = playbackStateRequests
    .then(async () => {
      const response = await fetch(`/api/replays/${replayId}/playback-state`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Replay-Token": token },
        body: JSON.stringify({ audio_busy: audioBusy, revision }),
      });
      await responseJson(response);
    })
    .catch((error) => {
      console.error("Could not report the viewer audio queue.", error);
      if (revision === playbackStateRevision && state.replayId === replayId) {
        setTimeout(() => reportViewerPlaybackState(audioBusy), 1000);
      }
    });
}

audioQueue.onLineQueueChange = reportViewerPlaybackState;

function pausePlaybackAudio() {
  audioQueue.pause();
  crowdBed.pause();
}

function resumePlaybackAudioInGesture() {
  audioQueue.resumeInGesture();
  crowdBed.resumeInGesture().catch((error) => {
    showMessage(el("ask-message"), `Crowd audio unavailable: ${error.message}`, true);
  });
}

function startCrowdInGesture({ starting = false } = {}) {
  if (!starting && state.replayId && (
    state.userPaused || state.awayPaused || state.replayComplete || document.visibilityState === "hidden"
  )) return;
  crowdBed.startInGesture().catch((error) => {
    showMessage(el("ask-message"), `Crowd audio unavailable: ${error.message}`, true);
  });
}

function setCrowdButton(enabled) {
  const button = el("crowd-button");
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", `Crowd sound ${enabled ? "on" : "off"}`);
  button.title = `Crowd sound ${enabled ? "on" : "off"}`;
}

function replayButton(onClick) {
  const button = make("button", "text-button replay-audio", "▶ Replay");
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function whyButton(onClick) {
  const button = make("button", "text-button", "Why?");
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function attachLineAudio(card, lineId, audioId) {
  keepFeedPinned(() => {
    card.querySelector(".audio-status")?.remove();
    card.querySelector(".replay-audio")?.remove();
    card.querySelector(".line-actions").append(
      replayButton(() => audioQueue.enqueue({ id: audioId, kind: "replay", lineId })),
    );
  });
}

function fillEvidence(title, text, badge, factTexts, events) {
  el("evidence-title").textContent = title;
  el("evidence-line").textContent = text;
  el("evidence-badge").textContent = badge;
  const facts = el("evidence-facts");
  facts.replaceChildren(...factTexts.map((fact) => make("li", "", fact)));
  if (!facts.children.length) facts.append(make("li", "", "No analytics facts were cited."));
  const list = el("evidence-events");
  list.replaceChildren(...events.map((event) => make("li", "", `${event.clock}' · ${eventText(event)}`)));
  if (!list.children.length) list.append(make("li", "", "No event references were attached."));
  el("evidence-dialog").showModal();
}

async function showEvidence(line) {
  showMessage(el("ask-message"), "");
  try {
    const result = await responseJson(await fetch(`/api/lines/${encodeURIComponent(line.line_id)}/evidence`));
    fillEvidence(
      "Line evidence",
      result.line.text,
      `${result.agent === "fallback" ? "Written from verified facts" : "Pundit line"} · ${result.evidence?.passed ? "evidence passed" : "evidence unavailable"}`,
      result.facts.map((fact) => `${fact.label}: ${fact.value}${fact.unit ? ` ${fact.unit}` : ""}`),
      result.events,
    );
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
  }
}

// An answer's evidence comes from its own payload and the events this page has received.
function showAnswerEvidence(result) {
  const explanation = result.explanation;
  const events = (explanation.event_ids || [])
    .map((id) => state.eventsById.get(id))
    .filter(Boolean);
  const checked = result.source === "agent" || result.source === "cached";
  fillEvidence(
    "Answer evidence",
    explanation.answer,
    checked ? "Pundit answer · evidence passed" : "Verified match facts",
    (explanation.claims || []).map((claim) => claim.text),
    events,
  );
}

const TIER_LABELS = {
  goal_peak: "Goal", goal_equaliser: "Equaliser", goal: "Goal", big_chance: "Big chance",
  red_card: "Red card", formation: "Change of shape", tactical: "Tactical",
  substitution: "Substitution", booking: "Booking", routine: "Commentary",
};

function lineMinute(line) {
  const clocks = (line.event_ids || [])
    .map((id) => state.eventsById.get(id)?.clock)
    .filter((clock) => clock != null);
  return clocks.length ? `${Math.max(...clocks)}'` : "";
}

function renderCommentary(payload) {
  const line = payload.line;
  const card = make("article", "feed-card commentary-card");
  const top = make("div", "card-top");
  const badge = make("span", `source-badge${line.source === "fallback" ? " fallback" : ""}`,
    TIER_LABELS[line.delivery_tier] || "Commentary");
  top.append(badge, make("span", "line-clock", lineMinute(line)));
  const actions = make("div", "line-actions");
  actions.append(whyButton(() => showEvidence(line)));
  card.append(top, make("p", "", line.text), actions);
  card.dataset.lineId = line.line_id;
  card.dataset.tier = line.delivery_tier || "routine";
  card.dataset.arrivedAt = String(Date.now());
  card.dataset.pausedAtArrival = String(audioQueue.pausedMs());
  if (payload.audio_id) {
    attachLineAudio(card, line.line_id, payload.audio_id);
  } else {
    actions.append(make("span", "line-clock audio-status",
      payload.audio_pending ? "Preparing audio…" : "Audio off"));
  }
  placeInFeed(card);
  if (payload.audio_id) {
    audioQueue.enqueue({
      id: payload.audio_id,
      kind: "line",
      lineId: line.line_id,
      tier: card.dataset.tier,
      expiresAt: Date.now() + LINE_AUDIO_STALE_MS,
    });
  } else if (payload.audio_pending) {
    audioQueue.setLinePending(line.line_id, true);
  }
  audioQueue.notifyLineQueueChange(true);
}

function updateCommentaryAudio(payload) {
  const card = [...document.querySelectorAll(".commentary-card")]
    .find((item) => item.dataset.lineId === payload.line_id);
  if (!card) return;
  attachLineAudio(card, payload.line_id, payload.audio_id);
  // Staleness counts from when the line's text arrived, not from when its audio did, and
  // leaves out any time paused since.
  const pausedSince = audioQueue.pausedMs() - Number(card.dataset.pausedAtArrival || 0);
  audioQueue.enqueue({
    id: payload.audio_id,
    kind: "line",
    lineId: payload.line_id,
    tier: card.dataset.tier,
    expiresAt: Number(card.dataset.arrivedAt) + LINE_AUDIO_STALE_MS + pausedSince,
  });
  audioQueue.notifyLineQueueChange(true);
}

// Q&A in the feed: a "YOU ASKED" card, then a Thinking… card that the answer replaces.

function questionCard(text) {
  const card = make("article", "feed-card question-card");
  card.append(make("span", "eyebrow", "YOU ASKED"), make("p", "question-text", text));
  return card;
}

function newQuestionId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replaceAll("-", "");
  return `q${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

// The first answer failed the evidence check and a second attempt is running.
function showCheckingFacts(card) {
  if (!card?.classList.contains("thinking")) return; // the answer is already showing
  const body = card.querySelector("p");
  body.textContent = "Checking the facts";
  body.append(make("span", "thinking-dots"));
}

function thinkingCard(text = "Thinking") {
  const card = make("article", "feed-card answer-card thinking");
  const top = make("div", "card-top");
  top.append(make("span", "eyebrow", "PUNDIT'S ANSWER"));
  const body = make("p", "", text);
  body.append(make("span", "thinking-dots"));
  card.append(top, body);
  return card;
}

function answerCard(result) {
  const card = make("article", "feed-card answer-card");
  const top = make("div", "card-top");
  top.append(make("span", "eyebrow", "PUNDIT'S ANSWER"));
  card.append(top, make("p", "", result.explanation.answer));
  const claims = result.explanation.claims || [];
  if (claims.length) {
    const list = make("ul", "answer-claims");
    list.append(...claims.map((claim) => make("li", "", claim.text)));
    card.append(list);
  }
  const blockMessages = {
    "betting language": "Betting language was blocked.",
    "a blocked real-club name": "A real club or league name was blocked.",
  };
  const notice = result.blocked
    ? claims.length
      ? "Some generated wording was withheld; verified match facts are shown."
      : blockMessages[result.blocked_reason] || "Some answer content was blocked."
    : result.explanation.notice || "";
  if (notice) card.append(make("p", "answer-notice", notice));
  const actions = make("div", "line-actions");
  actions.append(whyButton(() => showAnswerEvidence(result)));
  if (result.audio_pending) actions.append(make("span", "line-clock audio-status", "Preparing audio…"));
  card.append(actions);
  return card;
}

function attachAnswerAudio(card, audioId) {
  keepFeedPinned(() => {
    card.querySelector(".audio-status")?.remove();
    card.querySelector(".replay-audio")?.remove();
    card.querySelector(".line-actions")
      .append(replayButton(() => audioQueue.enqueue({ id: audioId, kind: "replay" })));
  });
  audioQueue.enqueue({ id: audioId, kind: "answer" });
}

function errorCard(message) {
  const card = make("article", "feed-card answer-card");
  card.append(make("p", "answer-notice", message));
  return card;
}

function renderRecap(payload) {
  const recap = payload.recap;
  const card = make("article", "feed-card recap-card");
  card.append(make("span", "eyebrow", "FULL TIME · THE FINAL WORD"), make("h3", "", recap.headline),
    make("p", "", recap.summary));
  const list = make("ol", "recap-moments");
  for (const moment of recap.key_moments) {
    const item = make("li");
    item.append(make("b", "", `${moment.minute}' `), document.createTextNode(moment.text));
    list.append(item);
  }
  card.append(list);
  if (recap.notice) card.append(make("p", "answer-notice", recap.notice));
  const actions = make("div", "line-actions");
  card.append(actions);
  card.id = "recap-card";
  placeInFeed(card);
  if (payload.audio_id) setRecapAudio(payload.audio_id);
  else if (payload.audio_pending) actions.append(make("span", "line-clock audio-status", "Preparing audio…"));
  else actions.append(make("span", "line-clock audio-status", "Audio unavailable · text recap shown"));
  setConnection("Full time · recap ready");
}

function setRecapAudio(audioId) {
  const card = el("recap-card");
  if (!card || !audioId) return;
  keepFeedPinned(() => {
    card.querySelector(".audio-status")?.remove();
    card.querySelector(".replay-audio")?.remove();
    card.querySelector(".line-actions")
      .append(replayButton(() => audioQueue.enqueue({ id: audioId, kind: "replay" })));
  });
  audioQueue.enqueue({ id: audioId, kind: "recap" });
}

function handleSse(name, payload) {
  if (name === "event") {
    renderEvent(payload);
    crowdBed.handleEvent(payload);
  }
  if (name === "commentary") renderCommentary(payload);
  if (name === "commentary_audio") updateCommentaryAudio(payload);
  if (name === "commentary_audio_unavailable") {
    audioQueue.setLinePending(payload.line_id, false);
    const card = [...document.querySelectorAll(".commentary-card")]
      .find((item) => item.dataset.lineId === payload.line_id);
    const notice = card?.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio off";
  }
  if (name === "recap") renderRecap(payload);
  if (name === "recap_audio") setRecapAudio(payload.audio_id);
  if (name === "recap_audio_unavailable") {
    const notice = el("recap-card")?.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio unavailable · text recap shown";
  }
  if (name === "complete") {
    state.replayComplete = true;
    crowdBed.pause();
    setConnection("Replay complete · audio remains available");
    el("start-button").disabled = false;
    el("start-button").textContent = "Start another match";
    el("match-picker").disabled = false;
    setPauseButton({ enabled: false });
    setPhase("done");
  }
  if (name === "error") showMessage(el("ask-message"), payload.message, true);
  if (name.startsWith("answer")) handleAnswerEvent(name, payload);
}

// Reads one connection of the replay's stream. Returns normally when the server closes it
// (it does so every few minutes, inside the ingress request timeout).
async function readStream(url, controller, stream) {
  const headers = { Accept: "text/event-stream", "X-Replay-Token": state.replayToken };
  if (stream.lastEventId) headers["Last-Event-ID"] = stream.lastEventId;
  const response = await fetch(url, { signal: controller.signal, headers, cache: "no-store" });
  if (!response.ok) {
    const error = new Error((await responseJson(response)).detail || "Could not open match stream.");
    error.status = response.status;
    throw error;
  }
  if (el("connection-state").textContent === "Reconnecting…") setConnection("Reconnected");
  // A dropped stream pauses the match on the server. If this page didn't mean to pause
  // (a blip, or the ingress cut), carry on; resuming a running replay changes nothing.
  if (stream.connections > 0 && !state.userPaused && !state.awayPaused && !state.replayComplete) {
    controlReplay("resume", { quiet: true });
  }
  stream.connections += 1;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let name = "message";
      let data = "";
      let id = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
        if (line.startsWith("id:")) id = line.slice(3).trim();
        if (line.startsWith("retry:")) stream.retryMs = Number(line.slice(6).trim()) || stream.retryMs;
      }
      if (data) handleSse(name, JSON.parse(data));
      // Only after the message is handled, so a drop mid-message resends it.
      if (id !== null) stream.lastEventId = id;
    }
  }
}

// Keeps the replay's stream open: on a clean close or a dropped connection it reconnects
// with Last-Event-ID, and the server resumes after the last message this page handled.
async function consumeStream(url, controller) {
  const stream = state.stream;
  let failures = 0;
  while (!controller.signal.aborted) {
    try {
      await readStream(url, controller, stream);
      failures = 0;
    } catch (error) {
      if (error.name === "AbortError") return;
      // 404: the replay has gone (stopped, or not reconnected in time). 400/409: don't retry.
      if (error.status && error.status < 500) throw error;
      failures += 1;
      setConnection("Reconnecting…", "reconnecting");
    }
    if (controller.signal.aborted) return;
    await new Promise((resolve) => setTimeout(resolve, stream.retryMs * Math.min(failures + 1, 5)));
  }
}

function resetMatch() {
  state.score = { home: 0, away: 0 };
  state.events = 0;
  state.eventsById.clear();
  state.stats = freshStats();
  state.pendingAnswers.clear();
  renderStats();
  el("home-score").textContent = "0";
  el("away-score").textContent = "0";
  el("clock").textContent = "PRE-MATCH";
  el("live-tag").textContent = "STANDBY";
  el("live-tag").classList.remove("on-air");
  el("key-moments").replaceChildren(
    make("li", "empty-state", "Goals, cards, substitutions and changes of shape appear here."));
  showSvg(el("pitch-ball"), false);
  showSvg(el("pitch-trail"), false);
  el("pitch-caption").textContent = "Waiting for kick-off";
  const empty = make("p", "empty-state",
    "Commentary will arrive with the match. The viewer feed runs on a short broadcast delay.");
  empty.id = "feed-empty";
  el("commentary").replaceChildren(empty);
  el("new-lines").hidden = true;
  audioQueue.clear();
  crowdBed.resetEventCues();
  el("question-input").value = "";
  autosizeQuestion();
  updateCharacterCount();
}

// Opens (or reopens, after the page was hidden) the replay's stream from where it left off.
function openStream() {
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  const replayId = state.replayId;
  consumeStream(`/api/replays/${replayId}/stream`, controller).catch((error) => {
    if (error.name === "AbortError" || state.replayId !== replayId) return;
    if (error.status === 404) {
      showReplayEnded();
      return;
    }
    showMessage(el("ask-message"), error.message, true);
    setConnection("Stream disconnected", "lost");
  });
}

// The replay has gone (stopped after too long away, or replaced): say so, never stop silently.
function showReplayEnded() {
  if (state.controller) state.controller.abort();
  state.controller = null;
  state.replayId = null;
  state.replayToken = null;
  state.awayPaused = false;
  el("welcome-back")?.remove();
  audioQueue.clear();
  crowdBed.pause();
  setPauseButton({ enabled: false });
  el("question-input").disabled = true;
  el("mic-button").disabled = true;
  el("ask-button").disabled = true;
  el("match-picker").disabled = false;
  el("start-button").disabled = false;
  el("start-button").textContent = "Start replay";
  el("live-tag").textContent = "ENDED";
  el("live-tag").classList.remove("on-air");
  setConnection("This replay has ended", "lost");
  setPhase("done");
  if (el("replay-ended")) return;
  const card = make("article", "feed-card answer-card notice-card");
  card.id = "replay-ended";
  card.append(make("p", "", "This replay has ended."));
  const again = make("button", "button button-primary", "Start again");
  again.type = "button";
  again.addEventListener("click", () => {
    audioQueue.unlock(); // a click, so autoplay stays allowed
    startReplay();
  });
  card.append(again);
  placeInFeed(card, { forceScroll: true });
}

// The viewer is back after the page was hidden while the match ran.
function showWelcomeBack() {
  if (el("welcome-back")) return;
  const card = make("article", "feed-card answer-card notice-card");
  card.id = "welcome-back";
  card.append(make("p", "", "Welcome back. The match paused while you were away."));
  const resume = make("button", "button button-primary", "Tap to resume");
  resume.type = "button";
  resume.addEventListener("click", resumeFromAway);
  card.append(resume);
  placeInFeed(card, { forceScroll: true });
}

// Inside the tap: audio first (iOS unlock), then the match.
function resumeFromAway() {
  el("welcome-back")?.remove();
  state.awayPaused = false;
  state.userPaused = false;
  resumePlaybackAudioInGesture();
  controlReplay("resume");
}

let closedWhileHidden = false;

// Leaving (lock screen, app switch, tab switch): pause the match before the connection goes.
// keepalive lets the request finish while the page is being frozen.
function pageHidden() {
  if (!state.replayId || closedWhileHidden) return;
  closedWhileHidden = true;
  const running = !state.replayComplete && !state.userPaused && !state.awayPaused;
  if (running) {
    state.awayPaused = true;
    pausePlaybackAudio();
    fetch(`/api/replays/${state.replayId}/control`, {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify({ action: "pause" }),
    }).catch(() => {}); // if it doesn't arrive, the server pauses when the stream drops
    setPauseButton({ paused: true });
    el("live-tag").textContent = "PAUSED";
  } else crowdBed.pause();
  // Close the stream ourselves rather than leave a frozen socket; it resumes on return.
  if (state.controller) state.controller.abort();
  state.controller = null;
}

function pageShown() {
  if (!closedWhileHidden) return;
  closedWhileHidden = false;
  if (!state.replayId) return;
  if (state.awayPaused) {
    setConnection("Paused while you were away");
    showWelcomeBack();
  }
  openStream();
}

async function startReplay() {
  showMessage(el("ask-message"), "");
  try {
    if (state.controller) {
      state.controller.abort();
      await new Promise((resolve) => setTimeout(resolve, 180));
    }
    resetMatch();
    el("replay-ended")?.remove();
    state.replayComplete = false;
    const matchId = el("match-picker").value;
    const speed = el("speed-picker").value;
    const profile = new URLSearchParams({ speed, mode: state.profile.mode, team: state.profile.team });
    const started = await responseJson(await fetch(`/api/matches/${matchId}/start?${profile}`, {
      method: "POST",
      headers: { "X-Client-ID": clientId },
    }));
    state.replayId = started.replay_id;
    state.replayToken = started.replay_token;
    if (started.profile) {
      state.profile = { mode: started.profile.mode, team: started.profile.team };
      renderProfile();
    }
    playbackStateRevision = 0;
    state.audio = started.audio !== false;
    el("match-picker").disabled = true;
    el("start-button").disabled = true;
    el("start-button").textContent = "Replay running";
    setPauseButton({ paused: false });
    setPhase("running");
    el("question-input").disabled = false;
    el("mic-button").disabled = !state.audio;
    el("ask-button").disabled = false;
    setConnection(started.mode === "cached"
      ? `Connected · ${speed}× replay · pre-written commentary`
      : `Connected · ${speed}× replay`);
    if (started.notice) placeInFeed(errorCard(started.notice));
    state.stream = { lastEventId: null, retryMs: 1000, connections: 0 };
    state.userPaused = false;
    state.awayPaused = false;
    audioQueue.notifyLineQueueChange(true);
    openStream();
  } catch (error) {
    crowdBed.pause();
    if (error.code === "busy") {
      showBusy(error.message);
      return;
    }
    showMessage(el("ask-message"), error.message, true);
  }
}

// Every replay slot is in use: a friendly card with a retry button, not an error.
function showBusy(message) {
  state.replayId = null;
  state.replayToken = null;
  const card = make("article", "feed-card answer-card busy-card");
  card.append(make("p", "", message));
  const retry = make("button", "button button-primary", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => {
    audioQueue.unlock(); // a click, so autoplay stays allowed
    startCrowdInGesture({ starting: true });
    card.remove();
    startReplay();
  });
  card.append(retry);
  placeInFeed(card, { forceScroll: true });
  setConnection("The pundit is busy", "idle");
  el("start-button").disabled = false;
  el("match-picker").disabled = false;
  setPhase("idle");
}

async function controlReplay(action, { quiet = false } = {}) {
  if (!state.replayId) return;
  try {
    const body = await responseJson(await fetch(`/api/replays/${state.replayId}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify({ action }),
    }));
    if (body.state === "finished" || body.state === "stopped") {
      setPauseButton({ enabled: false });
      setConnection(body.state === "finished"
        ? "Replay finished · waiting for final updates"
        : "Replay stopped");
      return;
    }
    if (quiet && body.state !== "running") return;
    setPauseButton({ paused: body.state === "paused" });
    el("live-tag").textContent = body.state === "paused" ? "PAUSED" : "ON AIR";
    if (!quiet) setConnection(body.state === "paused" ? "Replay paused" : "Replay resumed");
  } catch (error) {
    if (error.status === 404) showReplayEnded();
    else if (!quiet) showMessage(el("ask-message"), error.message, true);
  }
}

function updateCharacterCount() {
  el("character-count").textContent = `${el("question-input").value.length} / ${state.questionLimit}`;
}

function autosizeQuestion() {
  const input = el("question-input");
  input.style.height = "";
  if (!input.value) return; // an empty box stays one line, whatever the placeholder's length
  input.style.height = `${Math.min(input.scrollHeight + 2, 110)}px`;
  input.style.overflowY = input.scrollHeight > 110 ? "auto" : "hidden";
}

async function askTyped(event) {
  event.preventDefault();
  const question = el("question-input").value.trim();
  if (!question || !state.replayId) return;
  const button = el("ask-button");
  button.disabled = true;
  el("question-input").value = "";
  autosizeQuestion();
  updateCharacterCount();
  showMessage(el("ask-message"), "");
  // The viewer just acted, so show their question and the wait even if they scrolled up.
  placeInFeed(questionCard(question), { forceScroll: true });
  const waiting = thinkingCard();
  placeInFeed(waiting, { forceScroll: true });
  // The page picks the question ID, so an "answer_checking" event on the stream can find
  // this card while the request is still waiting.
  const questionId = newQuestionId();
  state.pendingAnswers.set(questionId, waiting);
  try {
    const result = await responseJson(await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify({ replay_id: state.replayId, question, question_id: questionId }),
    }));
    const answered = answerCard(result);
    placeInFeed(answered, { replace: waiting });
    // Its audio follows on the SSE stream as "answer_audio", as for a spoken question.
    if (result.audio_pending) state.pendingAnswers.set(result.question_id, answered);
    else state.pendingAnswers.delete(questionId);
  } catch (error) {
    state.pendingAnswers.delete(questionId);
    placeInFeed(errorCard(error.message), { replace: waiting });
  } finally {
    button.disabled = false;
  }
}

function encodeWav(samples, sampleRate) {
  const bytesPerSample = 2;
  const buffer = new ArrayBuffer(44 + samples.length * bytesPerSample);
  const view = new DataView(buffer);
  const writeText = (offset, value) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)));
  writeText(0, "RIFF");
  view.setUint32(4, 36 + samples.length * bytesPerSample, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, samples.length * bytesPerSample, true);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * bytesPerSample, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

async function convertToWav(blob) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContextClass();
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    const source = decoded.getChannelData(0);
    const targetRate = 16000;
    const targetLength = Math.ceil(source.length * targetRate / decoded.sampleRate);
    const samples = new Float32Array(targetLength);
    for (let index = 0; index < targetLength; index += 1) {
      const sourceIndex = index * decoded.sampleRate / targetRate;
      const low = Math.floor(sourceIndex);
      const high = Math.min(low + 1, source.length - 1);
      const fraction = sourceIndex - low;
      samples[index] = source[low] * (1 - fraction) + source[high] * fraction;
    }
    return encodeWav(samples, targetRate);
  } finally {
    await context.close();
  }
}

async function sendSpokenQuestion(blob) {
  const asked = questionCard("Transcribing your question…");
  placeInFeed(asked, { forceScroll: true });
  const waiting = thinkingCard("Listening back");
  placeInFeed(waiting, { forceScroll: true });
  const form = new FormData();
  form.append("replay_id", state.replayId);
  form.append("audio", blob, "question.wav");
  try {
    // 202 with the transcript as soon as speech-to-text finishes; the answer and its audio
    // follow on the replay's SSE stream as "answer" and "answer_audio".
    const pending = await responseJson(await fetch("/api/ask-audio", {
      method: "POST",
      headers: { "X-Replay-Token": state.replayToken },
      body: form,
    }));
    asked.querySelector(".question-text").textContent = `“${pending.transcript}”`;
    const thinking = thinkingCard();
    placeInFeed(thinking, { replace: waiting });
    state.pendingAnswers.set(pending.question_id, thinking);
  } catch (error) {
    asked.querySelector(".question-text").textContent = "Spoken question";
    placeInFeed(errorCard(error.message), { replace: waiting });
  }
}

function handleAnswerEvent(name, payload) {
  const card = state.pendingAnswers.get(payload.question_id);
  if (!card) return;
  if (name === "answer_checking") showCheckingFacts(card);
  if (name === "answer") {
    const answered = answerCard(payload);
    placeInFeed(answered, { replace: card });
    state.pendingAnswers.set(payload.question_id, answered);
  }
  if (name === "answer_error") {
    placeInFeed(errorCard(payload.message), { replace: card });
    state.pendingAnswers.delete(payload.question_id);
  }
  if (name === "answer_audio") {
    attachAnswerAudio(card, payload.audio_id);
    state.pendingAnswers.delete(payload.question_id);
  }
  if (name === "answer_audio_unavailable") {
    const notice = card.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio unavailable · text answer shown";
    state.pendingAnswers.delete(payload.question_id);
  }
}

async function toggleRecording() {
  if (state.recording) {
    state.recording.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    state.recording = recorder;
    recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener("stop", async () => {
      clearTimeout(state.recordingTimer);
      stream.getTracks().forEach((track) => track.stop());
      state.recording = null;
      el("mic-button").setAttribute("aria-pressed", "false");
      el("mic-label").textContent = "Talk";
      el("mic-button").disabled = false;
      showMessage(el("ask-message"), "");
      try {
        const wav = await convertToWav(new Blob(chunks, { type: recorder.mimeType }));
        if (wav.size > state.questionAudioBytes) {
          throw new Error(`Recording exceeds the ${state.questionAudioBytes}-byte audio limit.`);
        }
        await sendSpokenQuestion(wav);
      } catch (error) {
        showMessage(el("ask-message"), error.message, true);
      }
    });
    recorder.start();
    el("mic-button").setAttribute("aria-pressed", "true");
    el("mic-label").textContent = "Stop";
    showMessage(el("ask-message"), `Listening · up to ${state.questionAudioSeconds} seconds`);
    state.recordingTimer = setTimeout(() => { if (state.recording?.state === "recording") state.recording.stop(); }, state.questionAudioSeconds * 1000);
  } catch (error) {
    showMessage(el("ask-message"), `Microphone unavailable: ${error.message}`, true);
  }
}

// Mobile: keep the shell inside the visual viewport, so the ask bar sits above the keyboard
// on browsers that overlay it instead of resizing the page (iOS Safari).
function fitToVisualViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const shell = el("app-shell");
  shell.style.setProperty("--app-height", `${Math.round(viewport.height)}px`);
  shell.style.transform = viewport.offsetTop ? `translateY(${Math.round(viewport.offsetTop)}px)` : "";
}

function setDrawer(open) {
  el("match-info").classList.toggle("open", open);
  el("info-toggle").setAttribute("aria-expanded", String(open));
}

el("match-picker").addEventListener("change", updateMatchLabels);
for (const select of document.querySelectorAll("[data-profile]")) {
  select.addEventListener("change", () => changeProfile({ [select.dataset.profile]: select.value }));
}
renderProfile();
el("start-button").addEventListener("click", () => {
  audioQueue.unlock(); // in the click itself, before any await, so autoplay is allowed
  startCrowdInGesture({ starting: true });
  startReplay();
});
el("pause-button").addEventListener("click", () => {
  if (el("pause-button").dataset.paused === "true") {
    resumeFromAway(); // the same as the Welcome back tap: audio, then the match
  } else {
    state.userPaused = true;
    pausePlaybackAudio();
    controlReplay("pause");
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") pageHidden();
  else pageShown();
});
// Safari can skip visibilitychange when a page goes into the back-forward cache.
window.addEventListener("pagehide", pageHidden);
window.addEventListener("pageshow", (event) => { if (event.persisted) pageShown(); });
el("enable-audio").addEventListener("click", () => {
  audioQueue.unlock().then(() => audioQueue.next());
});
el("ask-form").addEventListener("submit", askTyped);
el("question-input").addEventListener("input", () => {
  updateCharacterCount();
  autosizeQuestion();
});
// Enter sends; Shift+Enter starts a new line.
el("question-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    el("ask-form").requestSubmit();
  }
  if (event.key === "Escape") el("question-input").blur();
});
// "/" focuses the ask box from anywhere except another text field.
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  if (target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))) return;
  if (el("evidence-dialog").open || el("question-input").disabled) return;
  event.preventDefault();
  el("question-input").focus();
});
el("commentary").addEventListener("scroll", () => {
  feedFollowing = feedIsNearBottom();
  if (feedFollowing) el("new-lines").hidden = true;
}, { passive: true });
// The feed's height changes with the window, the drawer and the phone keyboard: a viewer
// following the newest line stays on it.
new ResizeObserver(() => { if (feedFollowing) scrollFeedToBottom(); }).observe(el("commentary"));
// The "/" shortcut hint only makes sense with a keyboard, so only on the desktop layout.
const desktopLayout = matchMedia("(min-width: 1024px)");
function setQuestionPlaceholder() {
  el("question-input").placeholder = desktopLayout.matches
    ? "Ask the pundit about this match…  (press / )"
    : "Ask about this match…"; // short enough for one line on a narrow phone
}
desktopLayout.addEventListener("change", setQuestionPlaceholder);
setQuestionPlaceholder();
el("new-lines").addEventListener("click", scrollFeedToBottom);
el("info-toggle").addEventListener("click", () => setDrawer(!el("match-info").classList.contains("open")));
el("mic-button").addEventListener("click", toggleRecording);
el("mute-button").setAttribute("aria-pressed", String(audioQueue.muted));
el("mute-button").addEventListener("click", () => {
  const muted = !audioQueue.muted;
  audioQueue.setMuted(muted);
  crowdBed.setMuted(muted);
  if (!muted) startCrowdInGesture();
  el("mute-button").setAttribute("aria-pressed", String(muted));
});
setCrowdButton(crowdBed.enabled);
el("crowd-button").addEventListener("click", () => {
  const enabled = !crowdBed.enabled;
  crowdBed.setEnabled(enabled);
  setCrowdButton(enabled);
  if (enabled) startCrowdInGesture();
});
el("close-evidence").addEventListener("click", () => el("evidence-dialog").close());
el("evidence-dialog").addEventListener("click", (event) => {
  if (event.target === el("evidence-dialog")) el("evidence-dialog").close();
});
window.visualViewport?.addEventListener("resize", fitToVisualViewport);
window.visualViewport?.addEventListener("scroll", fitToVisualViewport);
fitToVisualViewport();

checkWarmup();
