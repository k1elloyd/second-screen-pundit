# Review 2: Second Screen Pundit at Phase 3 (`932fe9f`)

**Scope.** I read `prompts/build-prompt-v2.md`, `docs/DECISIONS.md`, `docs/reviews/review-1-opus.md` and all of `src/`, including `web/`. There's only one earlier review in `docs/reviews/` and no debug notes, so this review builds on review 1 alone. I didn't change any code.

**How I tested.** I ran the server locally with the README command and live Foundry and Speech:
- **Scripted judge session.** m02 at 20x, with 7 typed questions, 1 push-to-talk question (a TTS-generated WAV), every commentary line and its audio, and the recap.
- **Second m02 run in Chrome** to check the page itself.
- **Stage-timing harness** (scratch script, not committed). It wraps the real pipeline and times each model round trip, each tool, the evidence check and TTS for 4 questions and 1 recap.
- **Lifecycle probes with curl:** a start with no stream, a disconnect, the limit of 3 and a same-tab restart.
- **Offline test suite:** 144 passed, 1 failed (finding 15).

**What works.**
- **Q&A cut-off:** Q&A reads `viewer_released_as_of`, not the agents' `as_of`.
- **Disconnect:** closing the SSE stream stops the replay, cancels its runs and frees the slot. I tested this.
- **Warm-up gating:** Start stays disabled until warm-up finishes, with a clear status chip.
- **Keyless:** nothing secret reaches the browser.
- **Safe rendering:** every value is inserted with `textContent`, and SSML is XML-escaped.
- **Missing audio:** the UI shows "text only" when audio is unavailable instead of failing.

---

## Critical

### 1. No viewer question got a model answer: 0 of 8 in the live run

| Viewer clock | Question | Time | What the judge saw |
| :-- | :-- | --: | :-- |
| 15' | Who has had the better of the chances so far? | 8.9 s | fallback ("generated answer was withheld") |
| 30' | How did Port Calder score their goal? | 10.6 s | fallback (time budget) |
| 42' | spoken: What changed for Port Calder after the red card? | 16.1 s | fallback (time budget) |
| 50' | What changed for Port Calder after the red card? | 7.7 s | fallback ("withheld") |
| 58' | What are the odds Merehaven win this? | 8.7 s | fallback ("withheld") |
| 66' | Is Merehaven's pressing better than Liverpool's? | 9.9 s | fallback ("withheld") |
| 74' | Why are Merehaven camped in the final third? | 8.9 s | fallback ("withheld") |
| 84' | What formation are Port Calder playing now? | 8.0 s | fallback (evidence failed) |

Every answer read "Live answers are unavailable; verified match facts follow." It was followed by the same five facts (score, home possession, shots), whatever the question was.

**Diagnosis.** The harness captured the exception text.

1. **The final JSON is cut off.** All three tool-using questions failed with `Invalid JSON: EOF while parsing a string at line 1 column 1238` (then 1372 and 1442). The second call stops partway through the `fact_ids`/`event_ids` arrays.
   - The Explainer's `max_tokens` is 512 (`clients.py:28`), and this includes reasoning.
   - It has no reasoning effort set, so the model default applies (`clients.py:113`).
   - The schema asks for IDs twice, per claim and again at the top level (`prompts.py:73-79`).
   - Fact IDs are long hashes such as `match_state:a77648e68deb474fa…`.
   - Review 1 #10 predicted this.
2. **Exceptions aren't retried.** In `answer_question` an exception hits `break` (`pipeline.py:1407-1416`), so one truncation goes straight to the fallback. DECISIONS ("Phase 2b review fixes") says Q&A retries once after an exception. The recap path does retry (`continue`, `pipeline.py:1609`).
3. **The budget is half the spec.** `EXPLAINER_TIMEOUT_SECONDS` defaults to 10 s (`config.py:89`), but the spec says 20 s including the retry. One tool round trip takes 6–10 s, so a retry can never fit.
4. **The fallback blocks itself.** Its notice includes the exception class: "The live explanation failed (ChatClientException)…" (`pipeline.py:1470`). The content filter treats `ChatClientException` as an unapproved proper name, so `_safe_question_explanation` replaces it with a second fallback. That's why 5 answers say "withheld" and `blocked: an unapproved proper name`. The model's text was never filtered: there was no model text.
5. **The model doesn't know the viewer's minute.** The prompt carries only match id, team names and the question (`pipeline.py:1393-1403`).
   - `match_state(minute)` needs a minute, so the model guesses. One call failed with `ValidationError` (log 09:57:42).
   - The formation answer failed on "ungrounded number '60'; '90'".
   - The model also fans out: 5–7 parallel tool calls against a limit of 4, including `xg_timeline` (5–8k characters, 0.9–1.4 s of CPU).

**Fix.**
- **Explainer settings:** `max_tokens` 1,500, `reasoning: {"effort": "low"}`, and a 20 s budget, as the spec says.
- **IDs:** the server derives the top-level `fact_ids` and `event_ids` from the claims. Cap the answer at 3 claims and 3 event IDs per claim. Give the model short fact aliases (`F1`…`F20`) and map them back on the server.
- **Retry on exceptions:** replace `break` with `continue` and feed the parse error back as `retry_feedback`.
- **Viewer snapshot:** put a deterministic snapshot in the prompt: viewer clock, score, both formations, shots, xG and the last 5 key moments, computed at `viewer_released_as_of` and with fact IDs. Most questions then need 0–1 tool calls.
- **Fewer tools:** drop `xg_timeline` from the Explainer's tools, or slim it to the last 3 buckets.
- **Clean notices:** never put exception class names in viewer-facing text. Log them, and show a fixed phrase.
- **Tests:** add a live gate (5 questions per match), as for Phase 2b, and record the model-answer rate in DECISIONS.

### 2. The Q&A content filter blocks ordinary football English

`content_filter.py:118-151`. I ran these through `viewer_output_block_reason`:

| Text | Result |
| :-- | :-- |
| "Port Calder's shape changed after the red card." | **blocked** (possessive: `Calder's` → `calder s`) |
| "Merehaven Athletic's pressing has been relentless." | **blocked** |
| "Merehaven have generated more xG since the red card." | **blocked** (the regex finds the capital `G` inside `xG`) |
| "Merehaven have the better chances, with 1.42 xG." | **blocked** |
| "It is the Expected Goals that tell the story." | **blocked** |
| "The stakes are high for Port Calder." / "…against the odds." | **blocked** as betting |

- **Why it's critical:** once finding 1 is fixed, most real answers mention xG or a possessive, and the judge would still get the fallback.

**Fix.**
- Match capitalised words only at a word boundary (`\b[A-Z]…`), so `xG` is never split.
- Strip possessives (`'s`, `’s`, trailing `'`) before comparing with the allowed names.
- Add a small football glossary to the allowlist (Expected Goals, VAR and similar).
- Drop "stakes" and "odds" from the betting regex unless they come with a betting word ("odds on", "price", "bookmaker").
- Write a test corpus: these sentences must pass, and finding 13's leaks must fail.

### 3. Formations go back to the starting shape at the second-half kick-off

`engine/generate.py:322-329` emits the 46' kickoff with `{"formation": info.home.formation}`. `_formation_at` (`tools/analytics.py:219-233`) treats a kickoff as a formation event.

- **Result:** in m02, `formation_at(home, 60)` returns **4-2-3-1** for a team that went to a 4-4-1 with 10 men at 39'. I reproduced this directly.
- **Scope:** the same applies to `match_state` formations, to Q&A and to the recap.
- **Why it's critical:** this fact is wrong, yet it passes the evidence check, which is the worst failure mode for "explainable" commentary. It also undercuts m02's storyline.

**Fix.**
- The second-half kickoff should carry each team's current formation, or no formation at all.
- `_formation_at` should only read `kickoff` events with `period == 1`.
- Add a test: m02 home formation at 60' is 4-4-1.

---

## High

### 4. Known issue: questions are slow. Where the time goes, and how to show the text quickly

**Measurements** (live server and harness; m02, `gpt-5.4-mini`, UK machine to Australia East Foundry):

| Stage | Time |
| :-- | :-- |
| STT, 3-second question | 2.8–4.5 s |
| Explainer call 1 (chooses tools) | 1.2–4.5 s |
| Tools, in parallel threads | 0.1–0.7 s each; `xg_timeline` 0.9–1.4 s. Parallel calls slow each other down (one Python process, one GIL) |
| Explainer call 2 (writes the answer) | 2.1–4.2 s, then truncated (finding 1) |
| Evidence check | 1–4 ms |
| Deterministic fallback build | about 250 ms (`match_state` + `key_moments`) |
| TTS of the answer | 1.8–2.1 s |
| Retry, when it happens | repeats both calls: +3–7 s |
| **Typed total** (live run) | 7.7–10.6 s |
| **Spoken total** (live run) | 16.1 s: STT, Explainer, then TTS, all before the response is sent |

The evidence check isn't the problem. The time goes on two sequential model round trips, plus STT and TTS for spoken questions, all inside one HTTP request.

**Proposal.**
1. **Make one model call the normal case.** With the viewer snapshot in the prompt (finding 1), most questions need no tools: one call of about 2–3 s.
2. **Send the text before the audio.** `/api/ask` and `/api/ask-audio` return `{question_id, transcript, explanation}` as soon as the evidence check passes, and TTS runs in the background. The audio arrives on the replay's existing SSE stream as `answer_audio {question_id, audio_id}`, through the audio queue in finding 5. The typed answer no longer waits for TTS.
3. **Show the transcript straight after STT.** Make `/api/ask-audio` return `202 {question_id, transcript}` once STT finishes. Push the answer as an `answer` SSE event and its audio as `answer_audio`. The judge sees "Heard: …" after about 3 s, then the answer.
4. **Answer out-of-scope questions without a model call.** Check the *question* first. Betting words ("odds", "bet", "price") or a denylisted name get an immediate fixed reply with no model call, for example: "I only talk about this match: no betting, and no real clubs." This is faster, cheaper and shows the guardrail working. At present the odds question took 8.7 s and returned a fact dump.
5. **Show progress in the UI:** "Reading the match…" then "Checking the evidence…", based on elapsed time. A blank 9 s wait reads as broken.

**Expected result:** typed text in about 3 s; spoken transcript in about 3 s and text in about 6 s, with audio about 2 s later.

### 5. Known issue: commentary audio needs a play button. Proposal: an audio queue

**Diagnosis.**
- Each card gets its own `<audio controls preload="none">` (`app.js:200-206`, `224-231`), and nothing calls `play()`. Browsers block autoplay without a user gesture, so playing it in place would not have worked anyway.
- Audio also arrives about 2.1–2.7 s after the text, because TTS starts only after the line is released (`api.py:392-409`).

**Proposal** (vanilla JS, about 80 lines):

```js
class AudioQueue {
  constructor() { this.el = new Audio(); this.items = []; this.playing = null;
                  this.muted = localStorage.getItem("pundit-muted") === "1";
                  this.el.addEventListener("ended", () => this.next());
                  this.el.addEventListener("error", () => this.next()); }
  unlock() {                       // call synchronously inside the Start click handler
    this.el.src = SILENT_MP3_DATA_URI; return this.el.play().catch(() => {});
  }
  enqueue({ id, url, kind, lineId, expiresAt }) {   // kind: "line" | "answer" | "recap"
    if (kind === "line") this.items = this.items.filter(i => i.kind !== "line" || i.expiresAt > Date.now()).slice(-1);
    const item = { id, url, kind, lineId, expiresAt };
    kind === "line" ? this.items.push(item) : this.items.unshift(item);  // answers and recap jump the queue
    if (!this.playing) this.next();
  }
  next() {
    this.playing = null; document.querySelector(".commentary-card.speaking")?.classList.remove("speaking");
    const item = this.items.shift(); if (!item) return;
    if (this.muted || (item.expiresAt && item.expiresAt < Date.now())) return this.next();
    this.playing = item; this.el.src = item.url;
    document.querySelector(`[data-line-id="${item.lineId}"]`)?.classList.add("speaking");
    this.el.play().catch(() => showEnableAudioButton());   // fallback when the unlock failed
  }
  setMuted(m) { this.muted = m; localStorage.setItem("pundit-muted", m ? "1" : "0");
                if (m) { this.el.pause(); this.items = []; this.next(); } }
  pause() { this.el.pause(); }  resume() { if (this.playing) this.el.play().catch(() => {}); }
}
```

**Rules.**
- **Unlock** on the Start click, in the same tick, before any `await`.
- **One element:** a single `<audio>` element means lines can never overlap.
- **Staleness:** a line expires a fixed wall-clock time after it arrives (about 12 s at 20x). At most one line waits behind the one playing, so the voice never falls far behind the timeline.
- **Priority:** answers and the recap go to the front but don't cut off the line that's playing.
- **Mute:** the existing checkbox calls `setMuted`, which clears the queue and keeps the setting in `localStorage`.
- **Replay controls:** Pause and Resume also pause and resume the queue.
- **Cards:** replace each card's native player with a small "▶ Replay" button that enqueues that line. Show a "speaking" highlight on the card that's playing.

**Server change.** Synthesise each line *before* waiting for its release:
- In `LiveDispatcher._run`, run TTS while the line waits in `wait_released`.
- Lines are usually ready several seconds before release, because of the 10 s broadcast delay.
- Text and audio then arrive together, and `commentary_audio` becomes the exception.

### 6. The recap always falls back

**What happened.** Both runs (live server and harness) gave "Verified match recap". Its summary was "The deterministic event log records the match's key turning points", and all three key moments read "A goal changes the shape of the match."

**Diagnosis from the harness.**
- **Attempt 1:** event IDs cited as `fact_ids` ("unknown fact_id: m02-e0463…").
- **Both attempts:** "score '2-1' does not match a cited score fact". The model wrote winner-first, and the fact is "1-2".
- **Attempt 2:** minutes "27th", "38", "39", "54", "82" ungrounded, because a minute is grounded only by a cited event or a `match_context` fact.
- **Cost:** the attempts took 19.7 s and used 622–682 of the 768-token cap.
- **On the live server**, the recap failed with `ChatClientException` instead (probably truncation).

The full-time route is where a judge decides whether the product works, and it currently ends on boilerplate.

**Fix.**
- Pass the deterministic `score_phrase` (for example "Merehaven Athletic win 2-1") and the key moments with their minutes and event IDs in the prompt. Tell the model to reuse them verbatim.
- Have the server move any event ID found in `fact_ids` across to `event_ids`.
- Raise the cap to 1,500 with `effort: low`.
- Make the fallback recap readable from facts: "Merehaven Athletic came from behind to win 2-1 at Saltmere Park. Port Calder led through …, went down to ten men at 38' …". Templates with player names, as `fallbacks.py` does for lines, would read far better than the current text.

### 7. One client can use all three replay slots, and orphaned replays keep spending

I tested both.

- **Starts without a stream.** I sent 3 `POST /start` with different `X-Tab-ID` values and opened no stream. All three returned 200, and a fourth got 503 "All 3 replay slots are in use".
  - Those replays made **16 model calls in 40 s** with no viewer.
  - At 1x they would hold the slots for about 95 minutes.
  - `X-Tab-ID` is chosen by the client, so it doesn't limit anything.
- **Tab lock.** If a tab's stream never opens (a network failure between start and stream, or a closed tab), that tab gets 409 "This tab already has a replay" for the whole match. I reproduced this with a same-tab restart.
- **Memory leak.** A replay that finishes with no stream is never removed from `sessions`, and its audio stays in memory (`api.py:495-496`; only the stream's `finally` calls `_stop_session`).

**Fix.**
- **Stream deadline:** stop the session if no stream connects within 10 s of `/start`. A per-session watchdog task, cancelled in `stream()`, is enough.
- **Per-client limit:** one active replay per client IP, using the real IP (finding 12). Starting a new replay from the same IP stops the old one rather than returning 409.
- **Reap finished sessions:** remove a completed session 5 minutes after `complete` whether or not a stream is open. Remove it immediately when its stream closes.
- **Tests:** add one for each of the three cases above.

### 8. Known issues: formations read as "four dash two dash three dash one", and scores as "one to one". Proposal: a spoken-text step

**Diagnosis.**
- `SpeechSynthesizer.synthesize_mp3` passes the display text straight to `build_ssml` (`speech.py:85-93`).
- The text contains `score_phrase` output ("Port Calder lead 1-0", "The score is level at 1-1"; `commentator.py:210-216`), formations ("from a 4-2-3-1 to a 4-4-1"), percentages ("65%"), and minutes as "At 27 minutes" or "in the 82nd minute".
- Azure reads `4-2-3-1` with "dash", and an `n-n` score as "n to n".

**Proposal.** Add a deterministic `agents/spoken.py` with `to_spoken(text: str) -> str`.
- **Where it runs:** call it in exactly one place, `SpeechSynthesizer.synthesize_mp3`, before `build_ssml`. That covers lines, answers and the recap.
- **What it doesn't touch:** displayed text, the evidence check and the stored `CommentaryLine` stay as they are.
- **Order of rules:** apply them most-specific first, so a formation is never read as a score.

| # | Rule | Pattern (dashes: `[-‐‑–—]`) | Output |
| :-- | :-- | :-- | :-- |
| 1 | Formation, 3–5 parts, each digit 1–6 | `\b[1-6](?:D[1-6]){2,4}\b` | digits as words joined by hyphens: `four-two-three-one` |
| 2 | Minute range | `\b(\d{1,3})\s*D\s*(\d{1,3})'` or followed by `\s*minutes?` | `\1 to \2 minutes` |
| 3 | Stoppage-time minute | `\b(45\|90)\+(\d{1,2})'` | `\1 plus \2` (preceded by "in the" → `the \2nd minute of stoppage time` is optional) |
| 4 | Minute mark | `\b(?:at\|in\|on)\s+(\d{1,3})'` and bare `\b(\d{1,3})'` | `in the {ordinal} minute` / `{ordinal} minute` |
| 5 | "At N minutes" | `\b[Aa]t (\d{1,3}) minutes\b(?!\s+(?:left\|remaining\|to go\|to play))` | `In the {ordinal} minute` (keeps the capital) |
| 6 | Score: two numbers 0–15, not followed by `'`, a dash or "minute" | `(?<![\d.'D])(\d{1,2})\s*D\s*(\d{1,2})(?![\d'D])(?!\s*min)` | words with 0 → `nil`: `one-nil`, `one-one`, `nil-nil`, `two-one`, `nil-two` |
| 7 | Percentage | `(\d+(?:\.\d+)?)\s*%` | `\1 per cent` |
| 8 | Ordinal helper | 1st/2nd/3rd/4th…, 11th–13th, 21st, 22nd, 23rd, 101st | used by rules 4–5 |

**Notes.**
- **Scores:** I've written equal scores the way you asked ("one-one"). Many British commentators also say "one-all", so if you prefer that, make it a single switch, `EQUAL_SCORE_STYLE = "same" | "all"`; "nil-nil" is the same either way.
- **xG:** check how Ryan reads it. If it comes out as letters, add rule 9: `\bxG\b` → `expected goals`.

**Tests** (`tests/test_spoken.py`, `pytest.mark.parametrize`):

| Input | Expected spoken text |
| :-- | :-- |
| `Port Calder lead 1-0` | `Port Calder lead one-nil` |
| `The score is level at 1-1` | `The score is level at one-one` |
| `The score is level at 0-0` | `The score is level at nil-nil` |
| `Merehaven Athletic lead 2-1.` | `Merehaven Athletic lead two-one.` |
| `Port Calder trail 0-2` | `Port Calder trail nil-two` |
| `It finished 10-0` | `It finished ten-nil` |
| `They lead 3–2` (en dash) | `They lead three-two` |
| `switch from a 4-2-3-1 to a 4-4-1` | `switch from a four-two-three-one to a four-four-one` |
| `now a 3-4-3, 2-1 up` | `now a three-four-three, two-one up` |
| `a 4-4-2 at 1-1` | `a four-four-two at one-one` |
| `65% possession, up from 44%` | `65 per cent possession, up from 44 per cent` |
| `54.6% of the ball` | `54.6 per cent of the ball` |
| `At 27 minutes, Port Calder have the opener` | `In the 27th minute, Port Calder have the opener` |
| `scored at 88'` | `scored in the 88th minute` |
| `in the 82nd minute` | unchanged |
| `on 21'`, `at 22'`, `at 23'`, `at 11'`, `at 12'`, `at 13'`, `at 1'` | `21st`, `22nd`, `23rd`, `11th`, `12th`, `13th`, `1st minute` |
| `at 90+3'` | `at 90 plus 3` |
| `possession in 60-75'` | `possession in 60 to 75 minutes` |
| `between 60-75 minutes` | `between 60 to 75 minutes`; the rule should also turn "between X to Y" into "between X and Y" |
| `they have 52 minutes left` | unchanged, and not "52nd minute" |
| `At 38 minutes left` (guard) | unchanged |
| `0.31 xG` | unchanged (or `0.31 expected goals` with rule 9) |
| `1st half`, `Port Calder FC` | unchanged |
| Idempotence: `to_spoken(to_spoken(x)) == to_spoken(x)` | holds for every row |
| Property: output contains no `\d\s*[-–]\s*\d` | holds for every row |

Also add one test asserting that `SpeechSynthesizer.synthesize_mp3` passes `to_spoken(text)` into the SSML, using a fake SDK as the existing speech tests do.

---

## Medium

### 9. A commentary line can reach the viewer before its event

- **Seen in the live log:** the 27' and 54' goal lines arrived in the same millisecond as the goal event, but *before* it ("LINE @viewer 27' … opener" then "EVENT 27 goal").
- **Cause:** the dispatcher waits on `BroadcastDelay.wait_released` (`dispatcher.py:250-252`). The viewer events go out in a separate `release_events` loop (`api.py:444-452`). `ReplaySession.release_condition` is notified but never awaited.
- **Effect:** the commentary says "the opener" while the scoreboard still shows 0-0.
- **Fix:** in `publish_outcome`, use `async with session.release_condition: await session.release_condition.wait_for(lambda: session.viewer_released_as_of >= outcome.event.seq)` before queueing the line.

### 10. Pause doesn't pause the viewer for 10 seconds

- **Cause:** `Replay.pause` stops *publishing*, but `BroadcastDelay` keeps releasing everything already published for `BROADCAST_DELAY_SECONDS`.
- **Effect:** at 20x, about 3 match minutes of events and lines keep arriving after the judge presses Pause, so it looks broken.
- **Fix:** give `BroadcastDelay` `pause()`/`resume()`. While paused, `wait_released` blocks; on resume, add the paused time to every pending release time. Call both from `/control`. Add a test that no viewer event is released while paused.

### 11. Work that blocks the event loop

| Where | Measured | Effect |
| :-- | :-- | :-- |
| `LiveDispatcher._on_event` at each 3-minute boundary (`SignalTracker.evaluate` plus `is_must_say` over the window) | median 0 ms, **p99 345 ms, max 402 ms**; 9.3 s per match | Each of 3 replays stalls SSE and every request for about 0.35 s every 9 s at 20x |
| `fallback_question` → `_deterministic_results`, inside the async handler | about 250 ms | On the loop for every fallback answer (currently all of them) |
| `generate_match` in `/start` (reads the cached file) | 82 ms | Small |
| Analytics tools | 0.1–1.4 s of CPU each, in `to_thread` | Doesn't block, but holds the GIL and slows other replays |
| Sync `DefaultAzureCredential` passed to the async `FoundryChatClient` | not measured (token refreshes are hourly) | Review 1 #11 is still open: check whether the hourly refresh runs `get_token` on the loop (2.6–5.8 s) |

**Fix.**
- Run boundary evaluation and fallback building with `await asyncio.to_thread(...)`.
- Make `SignalTracker.evaluate` incremental: keep running totals per team instead of rescanning `events[:as_of]` every 3 minutes.
- Use `azure.identity.aio.DefaultAzureCredential` for Foundry. Keep the sync one for Speech, which already runs in threads.

### 12. Rate limiting can be bypassed, and upload size isn't capped

- **`X-Forwarded-For`:** `_client_ip` uses the *first* entry (`api.py:97-103`), and a client can set it to anything. Container Apps ingress *appends* the real client address, so the trustworthy value is the last entry. One script with a random header gets unlimited questions, and so unlimited model spend.
  - Fix: use the right-most entry (or the right-most after N known proxies), and use the same function for the per-IP replay cap.
- **Upload size:** `/api/ask-audio` reads at most 1 MB + 1 from the upload, but Starlette has already parsed and spooled the whole multipart body before the handler runs.
  - Fix: reject on `Content-Length > 1.1 MB` in a small middleware for that route.
- **Limiter memory:** the limiter keeps a deque for every IP it has seen, for ever. Delete empty deques.

### 13. The content filter misses things, and checks only part of the output

These pass `viewer_output_block_reason`:
- "Mbappe would have scored that." A capitalised single word at the start of a sentence is exempt.
- "Barcelona-style passing from Merehaven."
- Any real name written in lower case that isn't one of the 7 hashed denylist entries.

**Scope gap.** The filter runs only on Q&A. Commentary lines (model-written) and the recap (`make_recap`) go to the viewer and to TTS without it. Non-negotiable 5 covers all output.

**Fix.**
- Check capitalised words wherever they appear, using an allowlist: match names, plus a bundled list of common English words (a few thousand), plus the football glossary from finding 2. Anything else in capitals fails.
- Pre-check the question (finding 4.4), and add the scope rule to `EXPLAINER_INSTRUCTIONS`: "This match only. No betting or odds. No real clubs, players or leagues; if asked, say so in `cannot_tell`."
- Apply the same check to lines and the recap as part of the evidence step, so a hit retries and then falls back.

### 14. What a judge sees

The live m02 run, plus the same match in Chrome.

- **Dead air at the start.** After Start, the timeline is empty for 10 s (the broadcast delay). The first commentary line came at viewer 24', about 80 s in. `resetMatch` empties the commentary panel, so even the "Commentary will arrive" empty state disappears.
  - Fix: keep the empty state, and add a "Broadcast delay: 10 s" note in the scoreboard.
  - Fix: add an immediate deterministic pre-match line ("Port Calder FC v Merehaven Athletic at Saltmere Park, a 4-2-3-1 against a 4-4-2"), spoken as the first queued audio.
- **Thin commentary.** 9 lines in 90 minutes, 4 of them fallbacks.
  - The red card was heard twice: 38' fast path, then 42' tactical fallback "Port Calder must reorganise a player short". The Phase 2c fix that stops formation changes being announced twice doesn't cover red cards.
  - Phrasing lapses: "they have 52 minutes left to see it out" after a 38' red card, and "a back four now. 51 minutes left."
- **Noisy timeline.** Every pass and carry is listed, about 5 rows a second at 20x, and goals scroll off within seconds.
  - Insight rows show "READ · Tactical beat: xg surge", which is internal wording.
  - Fix: default to key events and shots, with a "Show all events" toggle.
- **The pitch mini-map is missing.** It's in the stack spec ("a pitch mini-map … flips the away team's coordinates"), and there's no element for it.
- **Commentary cards** show `goal · fast_path` or `tactical · tactical` where the judge expects the minute. The amber **FALLBACK** badge draws attention to a failure.
  - Fix: show `27'` and the tier ("Goal"). Move the source to the evidence view ("Written from verified facts").
- **The evidence view** is the strongest feature, but it reads like a debug dump.
  - Facts show raw `fact_id` hashes.
  - Events read "27' · Port Calder FC · pass · m02-e0240", with no player or description.
  - The badge says "fallback line · evidence passed".
  - Fix: reuse `eventText()` for each event, give the evidence sample size ("5 of 212 passes"), and put IDs behind a "Technical detail" `<details>` element.
- **Answers have no evidence link.** The claims list shows text only, though `/api/ask` returns `event_ids` per claim. Add the same "Why?" view to answers. That is the product's main promise.
- **Error text reaches the viewer:**
  - "The live explanation failed (ChatClientException)"
  - "Replay worker failed (KeyError)"
  - "The full-time recap failed (…)"
  - "Speech question failed (ValueError)": a NoMatch from STT returns 502, not 422 (`speech.py:166-167` raises `ValueError`; `api.py:757` maps it to 502)

  Fix: use plain phrases and log the class.
- **Stuck controls.** If the stream drops, `startReplay`'s catch shows "Stream disconnected", but Start stays disabled ("Replay running") until the page is reloaded. Reset the controls when the stream fails.
- **The recap** lists raw event IDs in each key moment (`app.js:380`). Show the minute and text, and link to the evidence view.
- **Small UI defects:**
  - The character counter is hard-coded as "/ 300" (`app.js:706`).
  - Before matches load, the away label reads "SECOND SCREEN".
  - "Push to talk" works as click to start, click to stop.

### 15. Config drift from the spec, and a failing test

- **Budgets below the spec:** `LIVE_WINDOW_TIMEOUT_SECONDS` defaults to **8** s and `EXPLAINER_TIMEOUT_SECONDS` to **10** s (`config.py:88-89`). The spec says 15 s and 20 s.
- **Failing test:** `tests/test_broadcast.py::test_new_settings_default_and_environment_overrides` asserts 15, so the suite has one failure (144 passed, 1 failed).
- **Not recorded:** DECISIONS has no Phase 3 section, so neither the change nor its reason is written down.

**Fix:** restore the spec values, or record the measured reason in DECISIONS and update the test. Add a Phase 3 section with this review's measurements.

---

## Low

16. **The Explainer prompt has no scope or style rules** (`prompts.py:73-79`). It has no British English rule, nothing about score orientation and nothing about out-of-scope questions. Add them alongside finding 1.
17. **The page polls `/healthz` every 2.5 s for ever** (`app.js:718`). Stop polling once warm-up is ready, or every open tab keeps the container from scaling to zero (an open SSE stream does too, and that's fine).
18. **`QuestionRequest` has no length limits:** `replay_id` and `question` are unbounded strings until the handler checks them. Add `Field(max_length=…)`.
19. **`/api/lines/{line_id}/evidence` and `/api/audio/{id}` search every session.** IDs are UUIDs, so they're safe, but look them up through a dictionary.
20. **SSE queues are unbounded.** If a client stops reading, the queue grows. Bound it, and drop events when it's full.

---

## Security and rules summary

- **Secrets: pass.** Nothing secret is in tracked files, `.env` holds only names and endpoints, the browser receives no tokens, and Speech and Foundry use Entra.
- **Input validation: mostly pass.**
  - Checked: question length, audio bytes and duration, speed range, tab-ID length and control actions.
  - Open: the multipart size and `QuestionRequest` limits (findings 12 and 18).
- **Rate limits: fail in practice.**
  - The question limit can be bypassed through `X-Forwarded-For`.
  - `/start` has no per-client cap.
  - Orphaned replays spend model calls (findings 7 and 12).
- **Q&A filter: too strict and too loose.**
  - Too strict: it blocks xG, possessives and its own notices (finding 2).
  - Too loose: it misses names at the start of a sentence and "-style" compounds (finding 13).
  - Not applied to lines or the recap.
- **No real names: pass for data.**
  - Fictional squads, the denylist test and the "All fixtures and players are fictional" footer.
  - Open: model output, until finding 13 is fixed.

---

## Ready to deploy (Phase 4)

| Area | Status | Action |
| :-- | :-- | :-- |
| **Container image** | No `Dockerfile` yet | `python:3.12-slim`; install the Speech SDK's system libraries (`libssl3`, `ca-certificates`, `libasound2`, per the Learn Linux requirements); run as a non-root user. |
| **Working-directory paths** | `data/cached` (`pipeline.py:443`, `cache.py:34`) and `data/matches` (`generate.py:404`) are relative to the CWD. `generate_match` *writes* `data/matches/*.jsonl` at runtime. | Resolve them from a `DATA_DIR` setting (default: the repo root via `Path(__file__)`). Copy `data/cached/` into the image. Generate the three matches at build time, so a read-only or non-root filesystem works and the first `/start` doesn't generate. |
| **Audio storage** | MP3 bytes kept in memory per session, using a temp dir only during synthesis. About 3–4 MB per replay. | Fine for one replica once sessions are reaped (finding 7). Keep `/tmp` writable. |
| **In-memory state** | Sessions, limiter and warm-up live in the process | `maxReplicas: 1` and **one uvicorn worker**: no `--workers` and no gunicorn. A new revision or a restart ends every replay; say so in the README. |
| **Port and bind address** | README binds `127.0.0.1:8000` | Container command: `uvicorn second_screen_pundit.api:app --host 0.0.0.0 --port ${PORT:-8000}`, with a matching Bicep `targetPort`. Don't pass `--env-file`. |
| **Health checks** | `/healthz` always returns 200 and reports `warmup` | Use it for liveness and startup, with a generous startup probe: import takes about 4.6 s and warm-up about 10.5 s locally. Don't make readiness depend on warm-up, or judges get an ingress error instead of the "warming up" page. |
| **Cold start** | About 15 s locally from process start to "ready"; scale-to-zero adds image pull and start | README note (spec). Show a timer or estimate on the warm-up chip. |
| **SSE through ingress** | Heartbeat every 15 s; response headers set | Check the Container Apps ingress idle and request timeouts on Learn, as the spec requires, and record them in DECISIONS. |
| **Managed identity** | `DefaultAzureCredential()` with no client ID | Set `AZURE_CLIENT_ID` to the user-assigned identity, or use `ManagedIdentityCredential(client_id=…)` in Azure (review 1 #25, still open). |
| **Logging** | No log configuration, so app `INFO` logs (warm-up, `agent_call`, timings) are lost by default; only warnings appear | Configure logging at startup, together with `configure_azure_monitor`. The agent timings are the evidence for Phase 4. |
| **Cost guardrails** | Orphaned replays spend; the question limit can be bypassed | Fix findings 7 and 12 before the URL is public. |

---

## The top 8 fixes, in the order I'd do them

1. **Make Q&A answer (finding 1).**
   - Explainer: `max_tokens` 1,500, `effort: low`, budget 20 s, and a retry on exceptions.
   - IDs: server-derived top-level IDs and short fact aliases.
   - Put a viewer snapshot (clock, score, formations, key moments) in the prompt.
   - No exception names in viewer text.
   - Add a live 5-question gate.
2. **Fix the content filter's false positives (finding 2), and extend it to lines and the recap (finding 13).** Add a pass/fail corpus test. Without this, fix 1 still shows the fallback.
3. **Fix the second-half formation reset (finding 3).** It's a wrong fact that passes the evidence check.
4. **Send answer text before audio, and pre-check out-of-scope questions (finding 4).** Typed answers in about 3 s; transcript, then text, then audio over SSE.
5. **Add the audio queue, and synthesise lines before release (finding 5).** Autoplay unlocked on Start, no overlap, mute, answers first.
6. **Add the spoken-text step with its test table (finding 8).** One choke point in `SpeechSynthesizer`; displayed text unchanged.
7. **Harden the replay lifecycle and limits (findings 7, 12, 9 and 10).**
   - A stream deadline, one replay per IP, and reaping finished sessions.
   - The right-most `X-Forwarded-For` entry and a cap on body size.
   - Release each line after its event, and pause the broadcast delay.
8. **Make the recap succeed and fix the judge-facing UI (findings 6, 14 and 15).**
   - Recap: give the model the score phrase and key moments, raise its cap, and write a readable fallback.
   - Timeline: show key events only by default.
   - Add the mini-map.
   - Cards: show minutes, and make the evidence and answer views readable.
   - Restore the spec budgets and record Phase 3 in DECISIONS.
