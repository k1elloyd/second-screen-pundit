const el = (id) => document.getElementById(id);
const tabId = sessionStorage.getItem("pundit-tab-id") || crypto.randomUUID();
sessionStorage.setItem("pundit-tab-id", tabId);

const state = {
  warmup: "warming",
  replayId: null,
  controller: null,
  match: null,
  replayComplete: false,
  score: { home: 0, away: 0 },
  events: 0,
  recording: null,
  pendingQuestionId: null,
  questionAudioSeconds: 15,
  questionAudioBytes: 1_000_000,
};

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
  el("home-name").textContent = selected.dataset.home || "Home";
  el("away-name").textContent = selected.dataset.away || "Away";
  el("stadium").textContent = selected.dataset.stadium || "";
}

async function checkWarmup() {
  try {
    const health = await responseJson(await fetch("/healthz", { cache: "no-store" }));
    state.warmup = health.warmup;
    const limits = health.limits || {};
    const questionLimit = limits.question_characters || 300;
    state.questionAudioSeconds = limits.question_audio_seconds || 15;
    state.questionAudioBytes = limits.question_audio_bytes || 1_000_000;
    el("question-input").maxLength = questionLimit;
    el("character-count").textContent = `${el("question-input").value.length} / ${questionLimit}`;
    document.querySelector(".limit-note").textContent =
      `${limits.questions_per_minute || 6} questions / ${limits.question_rate_window_seconds || 60}s`;
    const status = el("warm-status");
    status.classList.toggle("ready", health.ready);
    status.classList.toggle("failed", health.warmup === "failed");
    status.lastElementChild.textContent =
      health.warmup === "ready" ? "Pundit ready" :
      health.warmup === "failed" ? "Pundit unavailable — check server logs" :
      "Pundit warming up…";
    el("match-picker").disabled = !health.ready || Boolean(state.replayId);
    el("start-button").disabled = !health.ready || (Boolean(state.replayId) && !state.replayComplete);
  } catch (error) {
    showMessage(el("connection-state"), error.message, true);
  }
}

function eventText(event) {
  const team = event.team_name ? `${event.team_name}: ` : "";
  const player = event.player_name ? `${event.player_name} ` : "";
  const labels = {
    kickoff: "Kick-off",
    pass: event.outcome === "success" ? "completed a pass" : "misplaced a pass",
    carry: event.outcome === "success" ? "carried the ball forward" : "lost the ball in a carry",
    shot: "had a shot",
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

function renderEvent(event) {
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
  const list = el("timeline");
  if (state.events === 0) list.replaceChildren();
  state.events += 1;
  const item = document.createElement("li");
  item.className = `timeline-item${["goal", "card", "substitution", "formation_change", "half_time", "full_time"].includes(event.type) ? " key-event" : ""}`;
  const time = document.createElement("span");
  time.className = "event-clock";
  time.textContent = `${event.clock}'`;
  const description = document.createElement("span");
  description.className = "event-description";
  description.textContent = eventText(event);
  item.append(time, description);
  list.prepend(item);
  while (list.children.length > 70) list.lastElementChild.remove();
  if (event.type === "full_time") {
    el("connection-state").textContent = "Full time · recap pending";
  }
}

function audioUrl(id) {
  return id ? `/api/audio/${encodeURIComponent(id)}` : "";
}

async function showEvidence(line) {
  const dialog = el("evidence-dialog");
  showMessage(el("ask-message"), "");
  try {
    const result = await responseJson(await fetch(`/api/lines/${encodeURIComponent(line.line_id)}/evidence`));
    el("evidence-line").textContent = result.line.text;
    el("evidence-badge").textContent = `${result.agent} line · ${result.evidence?.passed ? "evidence passed" : "evidence unavailable"}`;
    const facts = el("evidence-facts");
    facts.replaceChildren();
    for (const fact of result.facts) {
      const li = document.createElement("li");
      li.textContent = `${fact.label}: ${fact.value}${fact.unit ? ` ${fact.unit}` : ""} · ${fact.fact_id}`;
      facts.append(li);
    }
    if (!facts.children.length) {
      const li = document.createElement("li");
      li.textContent = "No analytics facts were cited on this line.";
      facts.append(li);
    }
    const events = el("evidence-events");
    events.replaceChildren();
    for (const event of result.events) {
      const li = document.createElement("li");
      li.textContent = `${event.clock}' · ${event.team_name || "Match"} · ${event.type} · ${event.event_id}`;
      events.append(li);
    }
    if (!events.children.length) {
      const li = document.createElement("li");
      li.textContent = "No event references were attached to this line.";
      events.append(li);
    }
    dialog.showModal();
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
  }
}

function renderCommentary(payload) {
  const list = el("commentary");
  if (list.querySelector(".empty-state")) list.replaceChildren();
  const card = document.createElement("article");
  card.className = "commentary-card";
  const top = document.createElement("div");
  top.className = "commentary-top";
  const badge = document.createElement("span");
  badge.className = `source-badge${payload.line.source === "fallback" ? " fallback" : ""}`;
  badge.textContent = payload.line.source === "fallback" ? "Fallback" : "Pundit";
  const minute = document.createElement("span");
  minute.className = "line-clock";
  minute.textContent = `${payload.line.delivery_tier.replaceAll("_", " ")} · ${payload.line.line_id.split("-").at(-1)}`;
  top.append(badge, minute);
  const text = document.createElement("p");
  text.textContent = payload.line.text;
  const actions = document.createElement("div");
  actions.className = "line-actions";
  const why = document.createElement("button");
  why.className = "text-button";
  why.textContent = "Why?";
  why.addEventListener("click", () => showEvidence(payload.line));
  actions.append(why);
  card.append(top, text, actions);
  if (payload.audio_id) {
    const player = document.createElement("audio");
    player.controls = true;
    player.preload = "none";
    player.src = audioUrl(payload.audio_id);
    player.muted = el("mute-audio").checked;
    card.append(player);
  } else {
    const audioNotice = document.createElement("span");
    audioNotice.className = "line-clock audio-status";
    audioNotice.textContent = payload.audio_pending
      ? "Preparing audio…"
      : "Audio unavailable · text only";
    card.append(audioNotice);
  }
  card.dataset.lineId = payload.line.line_id;
  list.prepend(card);
  while (list.children.length > 30) list.lastElementChild.remove();
}

function updateCommentaryAudio(payload) {
  const card = [...document.querySelectorAll(".commentary-card")]
    .find((item) => item.dataset.lineId === payload.line_id);
  if (!card) return;
  card.querySelector(".audio-status")?.remove();
  const player = document.createElement("audio");
  player.controls = true;
  player.preload = "none";
  player.src = audioUrl(payload.audio_id);
  player.muted = el("mute-audio").checked;
  card.append(player);
}

function renderRecap(payload) {
  const recap = payload.recap;
  el("recap-panel").hidden = false;
  el("recap-headline").textContent = recap.headline;
  el("recap-summary").textContent = recap.summary;
  const list = el("recap-moments");
  list.replaceChildren();
  for (const moment of recap.key_moments) {
    const li = document.createElement("li");
    li.textContent = `${moment.minute}' · ${moment.text} · ${moment.event_ids.join(", ")}`;
    list.append(li);
  }
  el("recap-notice").textContent = recap.notice || (
    payload.audio_pending
      ? "Preparing audio…"
      : !payload.audio_id
        ? "Audio unavailable · text recap shown."
        : ""
  );
  const player = el("recap-audio");
  player.hidden = !payload.audio_id;
  if (payload.audio_id) player.src = audioUrl(payload.audio_id);
  player.muted = el("mute-audio").checked;
  el("connection-state").textContent = "Full time · recap ready";
}

function handleSse(name, payload) {
  if (name === "event") renderEvent(payload);
  if (name === "insight") {
    const item = document.createElement("li");
    item.className = "timeline-item";
    const time = document.createElement("span");
    time.className = "event-clock";
    time.textContent = "READ";
    const text = document.createElement("span");
    text.className = "event-description";
    text.textContent = payload.insight.headline;
    item.append(time, text);
    el("timeline").prepend(item);
  }
  if (name === "commentary") renderCommentary(payload);
  if (name === "commentary_audio") updateCommentaryAudio(payload);
  if (name === "commentary_audio_unavailable") {
    const card = [...document.querySelectorAll(".commentary-card")]
      .find((item) => item.dataset.lineId === payload.line_id);
    const notice = card?.querySelector(".audio-status");
    if (notice) notice.textContent = "Audio unavailable · text only";
  }
  if (name === "recap") renderRecap(payload);
  if (name === "recap_audio") {
    const player = el("recap-audio");
    player.hidden = false;
    player.src = audioUrl(payload.audio_id);
    player.muted = el("mute-audio").checked;
    el("recap-notice").textContent = "";
  }
  if (name === "recap_audio_unavailable") {
    el("recap-notice").textContent = "Audio unavailable · text recap shown.";
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

async function consumeStream(url, controller) {
  const response = await fetch(url, { signal: controller.signal, headers: { Accept: "text/event-stream" } });
  if (!response.ok) throw new Error((await responseJson(response)).detail || "Could not open match stream.");
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
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) handleSse(name, JSON.parse(data));
    }
  }
}

function resetMatch() {
  state.score = { home: 0, away: 0 };
  state.events = 0;
  el("home-score").textContent = "0";
  el("away-score").textContent = "0";
  el("clock").textContent = "PRE-MATCH";
  el("live-tag").textContent = "STANDBY";
  el("live-tag").classList.remove("on-air");
  el("timeline").replaceChildren();
  el("commentary").replaceChildren();
  el("recap-panel").hidden = true;
  el("answer-card").hidden = true;
  el("question-input").value = "";
  el("character-count").textContent = "0 / 300";
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
      headers: { "X-Tab-ID": tabId },
    }));
    state.replayId = started.replay_id;
    el("match-picker").disabled = true;
    el("start-button").disabled = true;
    el("start-button").textContent = "Replay running";
    el("pause-button").disabled = false;
    el("resume-button").disabled = true;
    el("question-input").disabled = false;
    el("mic-button").disabled = false;
    el("ask-button").disabled = false;
    el("connection-state").textContent = `Connected · ${speed}× replay`;
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
      headers: { "Content-Type": "application/json" },
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

function displayAnswer(result) {
  el("answer-card").hidden = false;
  el("answer-text").textContent = result.explanation.answer;
  const claims = el("answer-claims");
  claims.replaceChildren();
  for (const claim of result.explanation.claims || []) {
    const item = document.createElement("li");
    item.textContent = claim.text;
    claims.append(item);
  }
  claims.hidden = claims.childElementCount === 0;
  el("answer-transcript").textContent = result.transcript ? `Heard: “${result.transcript}”` : "";
  const player = el("answer-audio");
  player.hidden = !result.audio_id;
  if (result.audio_id) player.src = audioUrl(result.audio_id);
  player.muted = el("mute-audio").checked;
  const blockMessages = {
    "betting language": "Betting language was blocked.",
    "a blocked real-club name": "A real club or league name was blocked.",
  };
  const notice = result.blocked
    ? claims.childElementCount
      ? "Some generated wording was withheld; verified match facts are shown."
      : blockMessages[result.blocked_reason] || "Some answer content was blocked."
    : result.explanation.notice || (result.audio_pending ? "Preparing audio…" : "");
  showMessage(
    el("ask-message"),
    notice,
    result.blocked && claims.childElementCount === 0,
  );
}

async function askTyped(event) {
  event.preventDefault();
  const question = el("question-input").value.trim();
  if (!question || !state.replayId) return;
  const button = el("ask-button");
  button.disabled = true;
  showMessage(el("ask-message"), "Checking the released match events…");
  try {
    const result = await responseJson(await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replay_id: state.replayId, question }),
    }));
    displayAnswer(result);
    el("question-input").value = "";
    el("character-count").textContent = "0 / 300";
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
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
  showMessage(el("ask-message"), "Transcribing your question…");
  const form = new FormData();
  form.append("replay_id", state.replayId);
  form.append("audio", blob, "question.wav");
  try {
    // 202 with the transcript as soon as speech-to-text finishes; the answer and its audio
    // follow on the replay's SSE stream as "answer" and "answer_audio".
    const pending = await responseJson(await fetch("/api/ask-audio", { method: "POST", body: form }));
    state.pendingQuestionId = pending.question_id;
    el("answer-card").hidden = false;
    el("answer-text").textContent = "";
    el("answer-claims").replaceChildren();
    el("answer-claims").hidden = true;
    el("answer-audio").hidden = true;
    el("answer-transcript").textContent = `Heard: “${pending.transcript}”`;
    showMessage(el("ask-message"), "Checking the released match events…");
  } catch (error) {
    showMessage(el("ask-message"), error.message, true);
  }
}

function handleAnswerEvent(name, payload) {
  if (payload.question_id !== state.pendingQuestionId) return;
  if (name === "answer") displayAnswer(payload);
  if (name === "answer_error") showMessage(el("ask-message"), payload.message, true);
  if (name === "answer_audio") {
    const player = el("answer-audio");
    player.src = audioUrl(payload.audio_id);
    player.muted = el("mute-audio").checked;
    player.hidden = false;
    if (el("ask-message").textContent === "Preparing audio…") showMessage(el("ask-message"), "");
  }
  if (name === "answer_audio_unavailable" && el("ask-message").textContent === "Preparing audio…") {
    showMessage(el("ask-message"), "Audio unavailable · text answer shown.");
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
      el("mic-label").textContent = "Push to talk";
      el("mic-button").disabled = false;
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
    el("mic-label").textContent = "Stop recording";
    showMessage(el("ask-message"), `Listening · up to ${state.questionAudioSeconds} seconds`);
    state.recordingTimer = setTimeout(() => { if (state.recording?.state === "recording") state.recording.stop(); }, state.questionAudioSeconds * 1000);
  } catch (error) {
    showMessage(el("ask-message"), `Microphone unavailable: ${error.message}`, true);
  }
}

el("match-picker").addEventListener("change", updateMatchLabels);
el("start-button").addEventListener("click", startReplay);
el("pause-button").addEventListener("click", () => controlReplay("pause"));
el("resume-button").addEventListener("click", () => controlReplay("resume"));
el("ask-form").addEventListener("submit", askTyped);
el("question-input").addEventListener("input", () => {
  el("character-count").textContent = `${el("question-input").value.length} / 300`;
});
el("mic-button").addEventListener("click", toggleRecording);
el("mute-audio").addEventListener("change", () => {
  document.querySelectorAll("audio").forEach((player) => { player.muted = el("mute-audio").checked; });
});
el("close-evidence").addEventListener("click", () => el("evidence-dialog").close());
el("evidence-dialog").addEventListener("click", (event) => {
  if (event.target === el("evidence-dialog")) el("evidence-dialog").close();
});

loadMatches().then(checkWarmup).catch((error) => showMessage(el("connection-state"), error.message, true));
setInterval(checkWarmup, 2500);
