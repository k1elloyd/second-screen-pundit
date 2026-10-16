# Review 1: Second Screen Pundit at Phase 2b (`c0d31ec`)

**Scope.** I covered the prompt (v2), DECISIONS, the Copilot instructions, `src/`, `tests/`, `data/cached/`, `infra/` and `third_party/`. Phase 3 and later (the API, UI, Bicep and tracing) aren't built yet. Findings about them are preconditions, not bugs.

**Where it's strong.**
- Tools are cut off at `as_of` throughout.
- No future events reach any model prompt.
- Retries share one deadline.
- The broadcast delay is tested.
- Access is keyless: no keys are in tracked files, and `.env` is ignored.
- Club and player names are invented, with a hashed denylist test.
- bbenz's MIT licence is retained.

## Critical

**1. The number check grounds numbers, not claims, so an ungrounded statistic can get through.**
`agents/evidence.py:306-322`, `:149-154`; fact labels in `tools/analytics.py:298-309, 345-348, 541-551`

- **What's wrong:**
  - Every number in a text part is checked against one bag: all values of all facts that part cites, plus the minutes of the events it cites.
  - Integers get a ±0.5 tolerance against any fact, including fractional ones. An xG of 2.6 grounds "3 goals", and an xG of 0.31 grounds "0".
  - Many labels carry no team, window or metric. `shot_summary` facts are just "Shots" and "On target"; `pressing_intensity` is "Defensive actions per minute"; `compare_windows` is "Before", "After" and "Change". The Commentator receives only `{fact_id, label, value, unit}` (`pipeline.py:659-668`), so it has to guess whose number it's reading.
  - Example: cite `match_state:score` "2-1" and `shot_summary:on_target` 1, then write "Rovers have had 2 shots and 1 on target". It passes, though neither statement is true.
- **Why it matters:** this is the product's central promise ("every claim links to its evidence") and non-negotiable 4. A judge who tries one adversarial question will find it.
- **Fix:**
  - Make labels self-describing: team name, window, metric, and player names instead of IDs.
  - Match on unit and precision. An integer mention only matches an integer-valued fact or a minute. A decimal must match at the precision written. A percentage only matches a `%` fact.
  - Optionally, require a number to sit within a few tokens of its fact's unit word.
  - Add a strict test corpus of near-miss fabrications.

**2. Fact IDs aren't unique within a run, so citations can point at the wrong value.**
`tools/analytics.py:132-148` (`fact_id=f"{tool}:{slug(label)}"`), `evidence.py:232-236`, `pipeline.py:654-658`

- **What's wrong:** two calls to the same tool in one run produce identical IDs with different values. Examples: `shot_summary` for home then away, two `possession_window` ranges, or `match_state` at two minutes. The evidence map keeps whichever came last. DECISIONS (Phase 2b) acknowledges the collision but fixed it only for tactical signals.
- **Why it matters:**
  - A line that cites the home value can be checked against the away value: either a false rejection, or a wrong value that passes.
  - The planned `/api/lines/{line_id}/evidence` view would show the wrong fact.
  - Retries make it worse, because `accumulated_results` spans both attempts.
- **Fix:** derive the ID from the tool, a hash of its parameters, and the label slug (e.g. `shot_summary[home,60-75]:shots`). Reject duplicate IDs in `check_evidence`, and add a test with two `shot_summary` calls.

## High

**3. Q&A and the recap skip their retry and deterministic fallback for any failure other than a timeout.**
`pipeline.py:887-892, 921-932` (Explainer), `:1065-1070, 1094-1104` (Recap), `:522-525` (live), `tools/runtime.py:55-66`

- **What's wrong:**
  - Only `TimeoutError`, and structured-output `ChatClientException`s, are handled.
  - A 429, a 5xx, a network error, or a `MiddlewareFailure` propagates out of `answer_question` and `produce_recap`.
  - `MiddlewareFailure` is raised when a tool returns error text instead of JSON, and that is the likely result when the model passes an out-of-range argument such as `minute=93`, since `parameters.py:22-35` caps minutes at 90 and 91.
  - In the live lane, the dispatcher swallows the error and the window is lost with no retry.
- **Why it matters:** the spec says that after a second failure the viewer gets "the deterministic facts with a visible notice". A transient throttle during judging becomes an error page.
- **Fix:**
  - Catch `Exception` around each attempt (not `CancelledError`), and feed a short reason into the retry.
  - On a second failure, return `_fallback_explanation` or `_fallback_recap`.
  - Make tools clamp out-of-range minutes and record the clamp in `caveats`, as the spec requires, instead of raising.
  - Add fake-agent tests for 429, middleware failure and invalid arguments.

**4. The live route isn't an Agent Framework Workflow, and the agents have little agency.**
`pipeline.py:527-732`, `:875`, `:1053`; `docs/DECISIONS.md:59`; `prompts.py:18-19`

- **What's wrong:**
  - The Director → Analyst → Commentator route is hand-written Python calling `agent.run()` directly.
  - The Explainer and Recap are each one-node `WorkflowBuilder` wrappers.
  - DECISIONS says `WorkflowBuilder`/`AgentExecutor` "composes the conditional Director → Analyst → Commentator live route". The code doesn't do that.
  - Must-say events bypass the Director and Analyst, mandatory signals bypass the Director, and code picks the windows. In the gate run the Director made 6 accept/decline calls per match and routed nothing. The Analyst is told to use "at most two tool calls" and mostly uses none.
- **Why it matters:** this is the Best Multi-Agent bid. A judge reading the code sees deterministic templates reworded by one model, and a documentation claim that doesn't match the code.
- **Fix:**
  - Build the live route as a real Workflow with conditional edges, with the Director's decision choosing the edge (skip, Analyst → Commentator, or straight to Commentator).
  - Give the Director a real choice: which candidate signal, which angle, defer or merge.
  - Once tool payloads are slim (finding 22), let the Analyst use tools.
  - Show the decision trail in the UI ("why this line was said").
  - Correct DECISIONS line 59.

**5. Agent runs aren't cancelled when a replay stops.**
`agents/dispatcher.py:72-75, 91, 156`

- **What's wrong:** `run()` ends with `await asyncio.gather(*self._tasks)`, so it waits for every in-flight fast and tactical run (up to 15 s each) after the source ends. There's no `cancel()`.
- **Why it matters:** the spec (prompt line 143) says runs are cancelled as soon as the SSE client disconnects. Abandoned tabs keep spending tokens, and the 3-replay cap counts replays, not the agent work still running.
- **Fix:** add `LiveDispatcher.cancel()`, which cancels its tasks and awaits them with `return_exceptions=True`. Call it when the replay stops. Test that no model call starts after `stop()`.

**6. The number extractor has bypasses.**
`evidence.py:23-28, 32, 74-76, 319-320`

- **What's wrong:**
  - **Bare decimals:** the regex lookbehind `(?<![\w.])` makes ".4" invisible, so "an xG of .4" is never checked.
  - **Digit ordinals:** "1st" to "10th" are exempt (inherited from bbenz), but the spec says digits are checked, so "his 3rd goal" passes ungrounded.
  - **Ordinal words:** "second goal", "third" are not covered at all.
  - **Shirt-number prefix:** any number after `No.` or `#` is skipped, whether or not it is a shirt number.
- **Why it matters:** each of these is a concrete way to state a fabricated number.
- **Fix:**
  - Allow a leading `.` decimal.
  - Drop the ordinal exemption, or limit it to "first half" and "second half".
  - Add ordinal words to the vocabulary.
  - Remove the `No.`/`#` exemption. The Commentator is already told to use names, so a shirt-number mention should fail.

**7. Viewer-driven output has no content guard.**
`pipeline.py:876-886`, `prompts.py:36-42`

- **What's wrong:** the viewer's question goes to the Explainer verbatim. Nothing checks the output for real club or player names, betting or odds language, or abuse. The evidence check only looks at numbers.
- **Why it matters:** non-negotiable 5, the out-of-scope list ("betting or odds of any kind") and basic demo safety. "Is Ashbridge better than <real club>?" will get the real name repeated back.
- **Fix:**
  - Run a deterministic output filter before release, using the existing hashed denylist. Hash normalised 1–3-word n-grams so the denylist stays hashed.
  - Add a betting keyword list.
  - Treat a hit as an evidence failure, so it retries and then falls back.
  - Add an explicit out-of-scope instruction. Confirm the Foundry content filter is on for the deployment.

**8. TTS plays to the default speaker.**
`agents/speech.py:43-45`

- **What's wrong:** `SpeechSynthesizer(speech_config=...)` with `audio_config` omitted uses the default speaker. Microsoft Learn's `SpeechSynthesizer` reference says: "If it is not provided, the default speaker device will be used… If it is None, the output audio will be dropped." The code still reads `result.audio_data`, so the MP3 gets written, but locally the audio also plays aloud.
- **Why it matters:** in the Container Apps image there is no audio device, so synthesis may fail or log errors on every line. The existing test uses a fake synthesizer, so it can't catch this.
- **Fix:** pass `audio_config=None`, and add an assertion in the speech test that it is passed.

## Medium

**9. The must-say decision reads the future.**
`pipeline.py:107-115`, `dispatcher.py:90, 147, 152`

- **What's wrong:** `is_must_say(event, events)` scans the whole match log for a later `goal` that references the shot. At the time of the shot, the code already "knows" it will be scored and stays silent.
- **Why it matters:** no model prompt sees this, but it breaks the `as_of` rule in control flow, and a judge reading the code will notice. Hold-out behaviour also depends on the generator emitting the goal 5 seconds later.
- **Fix:** pass only `events[:as_of]`. For scoring shots, delay the big-chance decision by N seconds of match time, or until the next event, then decide using only what has happened.

**10. Explainer and Recap token caps include reasoning, and neither has been measured live.**
`clients.py:24-30, 106-108`

- **What's wrong:** caps are 512 for the Explainer and 768 for the Recap, with no reasoning effort set, so the model default applies. DECISIONS already found that `max_tokens` counts reasoning, and the Commentator's 256 cap truncated. A 200-word summary, a 150-word script and three moments is about 600 output tokens before any reasoning.
- **Why it matters:** the likely result is truncation, a retry (double the cost), and then the robotic fallback "Verified match recap". DECISIONS has no live latency or token measurement for either role.
- **Fix:** set `reasoning: {"effort": "low"}` for both, raise the recap cap to about 1,500, and add a live gate (as in Phase 2b) for 5 questions and 1 recap per match.

**11. Blocking calls on the event loop.**
`gate.py:38`, `latency.py:119` (sync `azure.identity.DefaultAzureCredential` passed to the async `FoundryChatClient`), `speech.py:46` (`speak_text_async(...).get()`)

- **What's wrong:**
  - If the client calls a sync `get_token` on the loop, every token refresh freezes all replays and SSE streams. DECISIONS measured token acquisition at 2.6–5.8 s. Verify how the client handles a sync credential.
  - `.get()` blocks for the whole synthesis.
- **Why it matters:** this is fine in a CLI gate, but in Phase 3's single process it stalls heartbeats and broadcast timing.
- **Fix:** use `azure.identity.aio` credentials for Foundry, and run synthesis in `asyncio.to_thread`.

**12. The evidence cap turns "evidence" into an unlabelled sample.**
`tools/analytics.py:47-67, 145-147`

- **What's wrong:** each `ToolResult` keeps at most 5 distinct event IDs across all its facts.
  - In `match_state`, the score's goals use up the quota, so the cards, shots and possession facts often have `event_ids: []`.
  - In `xg_timeline`, most buckets have no evidence at all.
- **Why it matters:** the cap is spec-mandated (prompt line 159), but the planned evidence view would present 5 arbitrary passes as "the events behind" 54.6% possession, and many claims cite facts with no events.
- **Fix:** keep the cap but add `event_count` and `evidence_sample: true` to each `Fact`, and give each fact its own small quota instead of one shared per result. Say "5 of 212 passes" in the UI.

**13. Pass-network facts use player IDs.**
`tools/analytics.py:393-399`

- **What's wrong:** labels read "Passing pair ashbridge-p07 to ashbridge-p09". The Explainer can't name players, and may read the IDs aloud. `validate_plain_spoken_text` only blocks event and fact IDs.
- **Fix:** resolve player names in labels through `get_match_info`, and block `-p\d{2}` IDs in spoken text.

**14. The synthetic data contradicts its own storylines.**
`engine/generate.py:232-238`

- **What's wrong:**
  - `_random_play` picks from shirts 1–11 for all 90 minutes. The m02 player sent off at 38' (shirt 4) keeps passing, and in m01 the player substituted off at 66' keeps playing.
  - Substitutes (shirts 12–13) never appear after coming on.
  - No `save` events are ever generated.
  - There are about 46 shots per match.
  - Coordinates are uniform across the pitch, so pressing and high-turnover rates are mostly noise.
  - No stoppage time is ever generated (`added_minute` is always 0).
- **Why it matters:** the brief is "football-realistic" data. `pass_network` will show a sent-off player in passing pairs, and every stoppage-time code path is untested.
- **Fix:** track who is on the pitch per team (red cards and substitutions), add saves for on-target shots, weight coordinates by phase of play, lower the shot rate, and add 1–5 minutes of added time per half. Add a test that a sent-off or substituted-off player has no later events.

**15. Phase 3 preconditions are missing.**
`dispatcher.py:81-91`, `engine/broadcast.py:28`

- **What's wrong:**
  - Nothing triggers the Recap Producer on `full_time`, so the full-time route is unwired.
  - `BroadcastDelay` has no accessor for the viewer's released position; only a `released` dict that `events()` fills. The spec (prompt lines 219 and 268) requires Q&A to use that position. The easy mistake is to use `replay.as_of`, which runs about 10 seconds ahead and leaks events the viewer hasn't seen.
- **Fix:** add `BroadcastDelay.released_as_of` (the highest released `seq`), and a test that Q&A at time *t* can't cite an event that hasn't been released. Dispatch the recap on `full_time` with `as_of` set to that event's `seq`.

**16. The Commentator has no memory, so lines repeat.**
`pipeline.py:678-689`, `data/cached/m01.json` (`m01-l001` and `m01-l002` both describe the first goal)

- **What's wrong:** the Commentator never sees earlier lines, so it repeats score phrases and framing. The cached data already has duplicate lines for the same event.
- **Fix:** pass the last 3–5 released lines and say "don't repeat their framing". Deduplicate cached lines per event.

**17. Tool parameters reject stoppage time.**
`tools/parameters.py:22-35, 101`

- **What's wrong:** minutes are capped at 90 and 91 even though the clock (`minute + added_minute`) can reach 95 or more.
- **Fix:** allow up to 120 and clamp to `as_of`, as the spec says (see also finding 3).

**18. Cached Q&A matching is loose.**
`agents/cache.py:51`

- **What's wrong:** a `SequenceMatcher` ratio of 0.45 is low. Any question loosely similar in wording gets a cached answer to a different question, and it passes the evidence check because the numbers are grounded. There are only 2 cached questions per match (the spec asks for 10–15).
- **Fix:** raise the threshold to about 0.75, or use a keyword intent map, and expand the cached set when the evaluation cases are written.

**19. The Phase 2 gate pads its MP3.**
`agents/gate.py:109`

- **What's wrong:** the "2 minutes of commentary" is cached lines plus the first two repeated (`validated_text + validated_text[:2]`). It isn't live output.
- **Why it matters:** it weakens the gate evidence if anyone checks it.
- **Fix:** synthesise lines from a live replay and concatenate them, or state the padding plainly in DECISIONS.

## Low

20. **`line_id` uniqueness** (`pipeline.py:549, 766`; `data/cached/*.json`). The ID is `l{as_of}`, so a fast-path line and a tactical line with the same `as_of` collide. Cached lines use a separate `l001…` numbering. Fix: one per-replay counter or `l{as_of}-{lane}`, used consistently.
21. **Window clamping** (`tools/analytics.py:114-129, 343-344`). A clamped `end` is exclusive, so the latest minute's events are dropped. A start beyond `as_of` gives an empty window that returns confident zeros. Fix: make the clamp inclusive, and add `cannot_tell` for empty windows.
22. **Duplicated payloads** (`tools/analytics.py:279, 403, 464`). `table` repeats the facts for `xg_timeline` (up to 36 facts), `pass_network` and `key_moments`, roughly doubling those tool payloads. Fix: leave `table` out of the model-visible serialisation.
23. **Zero always grounded** (`evidence.py:315`). Adding `event.added_minute` (always 0) to the allowed numbers means "0" and "zero" are always grounded when any event is cited. Fix: add it only when it is greater than 0.
24. **Attribution detail** (`evidence.py:1-3`; `third_party/bbenz_grounding/grounding.py:18`). The header names the source but not the licence path, URL or upstream commit. The vendored copy can't be imported (`from ..schemas`). Fix: name the licence file and pin a commit SHA in the header. Mark the vendored file "reference copy, not imported".
25. **Production credential** (`gate.py:38` pattern, ahead of Phase 4). In Azure, `DefaultAzureCredential` only picks up a user-assigned identity if `AZURE_CLIENT_ID` is set. Learn advises a specific credential in production. Fix: use `ManagedIdentityCredential(client_id=...)` in Azure and `DefaultAzureCredential` locally.
26. **Fast lane** (`dispatcher.py:91, 205-209`). Concurrency is unbounded and lines can be released out of order. Fix: cap the lane and release lines in `seq` order.

## Security summary

- **Pass:**
  - No keys, connection strings or tokens are in tracked files. `.env` holds only endpoint and resource names and is ignored.
  - Foundry and Speech use only an Entra `TokenCredential`.
  - Logs record roles, timings and token counts, not prompts or questions.
- **Open:**
  - The content guard (finding 7).
  - The production credential type (finding 25).
  - Agent Framework telemetry isn't configured yet. When it is, keep `ENABLE_SENSITIVE_DATA=false`.

## Hackathon rules summary

- **Synthetic data:** pass. Clubs, stadiums and players are invented, there's a hashed denylist test, and there are no league references outside the prompt.
- **Attribution:** pass, with the minor fix in finding 24.
- **Risks:**
  - viewer-induced real names or betting talk (finding 7)
  - realism gaps a judge will spot (finding 14)

## Test gaps

- **Number check:**
  - a fabricated digit (the only "ungrounded" test uses the word "three")
  - ".4"
  - "No. 9" and "#9"
  - "3rd"
  - percentage rounding
  - formation and "back three" rules
  - score orientation in an Explanation or Recap
  - the near-miss corpus from finding 1
- **Fact-ID collision** within one run (finding 2).
- **Explainer and Recap in live mode with fake agents:** retry with feedback, timeout fallback, exception fallback, and the `_fallback_*` builders directly (finding 3).
- **Tool argument errors** and the `MiddlewareFailure` path.
- **Dispatcher cancellation** on stop, and no new calls after stop (finding 5).
- **`as_of` safety of control flow:** no decision may depend on `seq > as_of` (finding 9).
- **Viewer released-position cut-off** for Q&A, and **full time → recap** dispatch (finding 15).
- **Speech:** `audio_config=None` is passed (finding 8).
- **Output content guard** (finding 7).
- **Stoppage time** end to end; this needs the generator to emit added time (findings 14 and 17).
- **`line_id` uniqueness** across lanes (finding 20).

## What a judge would criticise in the agentic design

- **"Multi-agent" is mostly a deterministic pipeline.** Templated insights are reworded by one model call. The Director is a yes/no gate over about 6 candidates per match, and the Workflow appears only as single-node wrappers (finding 4).
- **The Analyst rarely analyses.** It is told to avoid tools because tool payloads were slow (findings 4 and 22).
- **No memory or narrative arc.** Lines don't build on each other (finding 16).
- **Grounding is numeric, not semantic.** Team attribution and claim meaning aren't checked (finding 1).
- **Q&A and recap are unmeasured live.** The fallback recap reads as boilerplate (finding 10).
- **UX is a quarter of the score and Phase 3 hasn't started.** The deadline is 27 October.
- **What will impress:** the `as_of` discipline, the deterministic evidence gate with retry, the broadcast-delay design, and the honest latency measurements in DECISIONS. Put these at the front of the demo.

## The 5 most important fixes

1. **Make fact IDs unique per call and labels self-describing** (team, window, metric, player names). This fixes finding 2 and most of finding 1.
2. **Tighten the number check:** match by unit and precision, close the ".4", ordinal and "No./#" holes, and add the near-miss test corpus (findings 1 and 6).
3. **Route every failure through retry and then the deterministic fallback,** and make tools clamp instead of raising (finding 3).
4. **Make the live route a real Agent Framework Workflow** with Director routing, and cancel runs on disconnect (findings 4 and 5).
5. **Add an output content guard** for real names and betting, and pass `audio_config=None` to the synthesizer (findings 7 and 8).
