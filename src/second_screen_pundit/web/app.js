const el = (id) => document.getElementById(id);
// This viewer's ID, kept for the tab's life: one replay per viewer, not per network address.
const clientId = sessionStorage.getItem("pundit-client-id") || crypto.randomUUID();
sessionStorage.setItem("pundit-client-id", clientId);

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
};

function freshStats() {
  const side = () => ({ shots: 0, target: 0, fouls: 0, cards: 0, formation: "–" });
  return { home: side(), away: side() };
}
state.stats = freshStats();

function showMessage(target, text, isError = false) {
  target.textContent = text || "";
  target.classList.toggle("error", isError);
}

async function responseJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : `Request failed (${response.status}).`;
    throw new Error(message);
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
  // Keep polling only until ready; a failed warm-up is retried slowly in case it recovers.
  if (!health.ready) setTimeout(checkWarmup, health.warmup === "failed" ? 15000 : WARMUP_POLL_MS);
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
  }
}

function countEvent(event) {
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
  el("live-light").classList.add("active");
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
  if (event.type === "full_time") {
    el("connection-state").textContent = "Full time · recap pending";
  }
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

// One <audio> element plays everything in turn, so lines never overlap. Lines wait in order;
// at most one waits behind the line that is playing. Answers, the recap and replays go to the
// front without cutting off the line that is playing.
class AudioQueue {
  constructor() {
    this.el = new Audio();
    this.items = [];
    this.playing = null;
    this.paused = false;
    this.muted = readStored("pundit-muted") === "1";
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

  enqueue({ id, kind, lineId = null, expiresAt = null }) {
    if (!id || this.muted) return;
    const item = { url: audioUrl(id), kind, lineId, expiresAt };
    if (kind === "line") {
      this.items = this.items
        .filter((queued) => queued.kind !== "line" || queued.expiresAt > Date.now())
        .filter((queued, index, all) => queued.kind !== "line" ||
          index === all.findLastIndex((other) => other.kind === "line"));
      this.items.push(item);
    } else {
      this.items.unshift(item);
    }
    if (!this.playing && !this.paused) this.next();
  }

  next() {
    this.playing = null;
    document.querySelector(".commentary-card.speaking")?.classList.remove("speaking");
    if (this.paused) return;
    const item = this.items.shift();
    if (!item) return;
    if (this.muted || (item.expiresAt && item.expiresAt < Date.now())) {
      this.next();
      return;
    }
    this.playing = item;
    this.el.src = item.url;
    if (item.lineId) {
      [...document.querySelectorAll(".commentary-card")]
        .find((card) => card.dataset.lineId === item.lineId)?.classList.add("speaking");
    }
    this.el.play().catch((error) => {
      if (error.name === "NotAllowedError") {
        // The unlock failed: keep the item and ask for a click.
        this.items.unshift(item);
        this.playing = null;
        el("enable-audio").hidden = false;
      } else {
        this.next();
      }
    });
  }

  setMuted(muted) {
    this.muted = muted;
    writeStored("pundit-muted", muted ? "1" : "0");
    if (muted) {
      this.el.pause();
      this.items = [];
      this.next();
    }
  }

  pause() {
    this.paused = true;
    this.el.pause();
  }

  resume() {
    this.paused = false;
    if (this.playing) this.el.play().catch(() => {});
    else this.next();
  }

  clear() {
    this.items = [];
    this.el.pause();
    this.playing = null;
    document.querySelector(".commentary-card.speaking")?.classList.remove("speaking");
  }
}

const audioQueue = new AudioQueue();

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
  card.dataset.arrivedAt = String(Date.now());
  if (payload.audio_id) {
    attachLineAudio(card, line.line_id, payload.audio_id);
  } else {
    actions.append(make("span", "line-clock audio-status",
      payload.audio_pending ? "Preparing audio…" : "Audio unavailable · text only"));
  }
  placeInFeed(card);
  if (payload.audio_id) {
    audioQueue.enqueue({
      id: payload.audio_id,
      kind: "line",
      lineId: line.line_id,
      expiresAt: Date.now() + LINE_AUDIO_STALE_MS,
    });
  }
}

function updateCommentaryAudio(payload) {
  const card = [...document.querySelectorAll(".commentary-card")]
    .find((item) => item.dataset.lineId === payload.line_id);
  if (!card) return;
  attachLineAudio(card, payload.line_id, payload.audio_id);
  // Staleness counts from when the line's text arrived, not from when its audio did.
  audioQueue.enqueue({
    id: payload.audio_id,
    kind: "line",
    lineId: payload.line_id,
    expiresAt: Number(card.dataset.arrivedAt) + LINE_AUDIO_STALE_MS,
  });
}

// Q&A in the feed: a "YOU ASKED" card, then a Thinking… card that the answer replaces.

function questionCard(text) {
  const card = make("article", "feed-card question-card");
  card.append(make("span", "eyebrow", "YOU ASKED"), make("p", "question-text", text));
  return card;
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
  el("connection-state").textContent = "Full time · recap ready";
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
  if (name === "event") renderEvent(payload);
  if (name === "commentary") renderCommentary(payload);
  if (name === "commentary_audio") updateCommentaryAudio(payload);
  if (name === "commentary_audio_unavailable") {
    const card = [...document.querySelectorAll(".commentary-card")]
      .find((item) => item.dataset.lineId === payload.line_id);
    const notice = card?.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio unavailable · text only";
  }
  if (name === "recap") renderRecap(payload);
  if (name === "recap_audio") setRecapAudio(payload.audio_id);
  if (name === "recap_audio_unavailable") {
    const notice = el("recap-card")?.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio unavailable · text recap shown";
  }
  if (name === "complete") {
    state.replayComplete = true;
    el("connection-state").textContent = "Replay complete · audio remains available";
    el("start-button").disabled = false;
    el("start-button").textContent = "Start another match";
    el("match-picker").disabled = false;
    el("pause-button").disabled = true;
    el("resume-button").disabled = true;
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
  if (el("connection-state").textContent === "Reconnecting…") el("connection-state").textContent = "Reconnected";
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
  const stream = { lastEventId: null, retryMs: 1000 };
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
      el("connection-state").textContent = "Reconnecting…";
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
  el("question-input").value = "";
  autosizeQuestion();
  updateCharacterCount();
}

async function startReplay() {
  showMessage(el("ask-message"), "");
  try {
    if (state.controller) {
      state.controller.abort();
      await new Promise((resolve) => setTimeout(resolve, 180));
    }
    resetMatch();
    state.replayComplete = false;
    const matchId = el("match-picker").value;
    const speed = el("speed-picker").value;
    const started = await responseJson(await fetch(`/api/matches/${matchId}/start?speed=${speed}`, {
      method: "POST",
      headers: { "X-Client-ID": clientId },
    }));
    state.replayId = started.replay_id;
    state.replayToken = started.replay_token;
    state.audio = started.audio !== false;
    el("match-picker").disabled = true;
    el("start-button").disabled = true;
    el("start-button").textContent = "Replay running";
    el("pause-button").disabled = false;
    el("resume-button").disabled = true;
    el("question-input").disabled = false;
    el("mic-button").disabled = !state.audio;
    el("ask-button").disabled = false;
    el("connection-state").textContent = started.mode === "cached"
      ? `Connected · ${speed}× replay · pre-written commentary`
      : `Connected · ${speed}× replay`;
    if (started.notice) placeInFeed(errorCard(started.notice));
    const controller = new AbortController();
    state.controller = controller;
    consumeStream(`/api/replays/${started.replay_id}/stream`, controller).catch((error) => {
      if (error.name !== "AbortError") {
        showMessage(el("ask-message"), error.message, true);
        el("connection-state").textContent = "Stream disconnected";
      }
    });
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
  }
}

async function controlReplay(action) {
  if (!state.replayId) return;
  try {
    const body = await responseJson(await fetch(`/api/replays/${state.replayId}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify({ action }),
    }));
    if (body.state === "finished" || body.state === "stopped") {
      el("pause-button").disabled = true;
      el("resume-button").disabled = true;
      el("connection-state").textContent = body.state === "finished"
        ? "Replay finished · waiting for final updates"
        : "Replay stopped";
      return;
    }
    el("pause-button").disabled = body.state !== "running";
    el("resume-button").disabled = body.state !== "paused";
    el("live-tag").textContent = body.state === "paused" ? "PAUSED" : "ON AIR";
    el("connection-state").textContent = body.state === "paused" ? "Replay paused" : "Replay resumed";
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
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
  try {
    const result = await responseJson(await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Replay-Token": state.replayToken },
      body: JSON.stringify({ replay_id: state.replayId, question }),
    }));
    const answered = answerCard(result);
    placeInFeed(answered, { replace: waiting });
    // Its audio follows on the SSE stream as "answer_audio", as for a spoken question.
    if (result.audio_pending) state.pendingAnswers.set(result.question_id, answered);
  } catch (error) {
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
el("start-button").addEventListener("click", () => {
  audioQueue.unlock(); // in the click itself, before any await, so autoplay is allowed
  startReplay();
});
el("pause-button").addEventListener("click", () => {
  audioQueue.pause();
  controlReplay("pause");
});
el("resume-button").addEventListener("click", () => {
  audioQueue.resume();
  controlReplay("resume");
});
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
    : "Ask the pundit about this match…";
}
desktopLayout.addEventListener("change", setQuestionPlaceholder);
setQuestionPlaceholder();
el("new-lines").addEventListener("click", scrollFeedToBottom);
el("info-toggle").addEventListener("click", () => setDrawer(!el("match-info").classList.contains("open")));
el("mic-button").addEventListener("click", toggleRecording);
el("mute-audio").checked = audioQueue.muted;
el("mute-audio").addEventListener("change", () => audioQueue.setMuted(el("mute-audio").checked));
el("close-evidence").addEventListener("click", () => el("evidence-dialog").close());
el("evidence-dialog").addEventListener("click", (event) => {
  if (event.target === el("evidence-dialog")) el("evidence-dialog").close();
});
window.visualViewport?.addEventListener("resize", fitToVisualViewport);
window.visualViewport?.addEventListener("scroll", fitToVisualViewport);
fitToVisualViewport();

checkWarmup();
