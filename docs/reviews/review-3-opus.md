# Review 3: pre-submission (`0847235`, 18 Oct 2026)

**Scope.** I read `prompts/build-prompt-v2.md`, `.github/copilot-instructions.md`, all of `docs/DECISIONS.md` and `docs/reviews/review-2-opus.md`, then `api.py`, `spending.py`, `content_filter.py`, `cache.py`, the wording, recap and colour paths, the page's warm-up and visibility code, and the infra. I didn't change any code.

**What I checked against the live system** (all read-only):
- **Live URL.** `/healthz`, `/api/matches` and `/docs`. The deployed `app.js` and `styles.css` are byte-identical to HEAD, so the colour-line commit is live.
- **Container app.** `az containerapp show`: min 1 and max 1 replica, `secrets` null, `MODEL_CALLS_PER_DAY=450`.
- **Cognitive Services accounts.** `az cognitiveservices account list` for `rg-pundit`.
- **Spending.** `az consumption budget list`, and month-to-date cost from the Cost Management API.
- **Repo history.** A key-pattern scan of `git log -p --all` (75 commits).
- **CI.** GitHub Actions on HEAD: green.
- **Colour lines.** I timed `build_colour_candidate` locally.

**Not checked.**
- **Official rules.** These were added after the first draft, from the link you sent; see "Official rules" near the end.
- **Real phones and a full live match.** I didn't run a full live match in a browser or test on a real phone; DECISIONS already records those gates.

---

## Must fix before the video

### 1. A failed warm-up leaves the app unusable until the next restart
- **Where:** `api.py:361-371`, `app.js:190-192`, `pipeline.py:1098-1112`.
- **Why it matters:**
  - **The server never retries.** It makes one warm-up attempt when the container starts. If that attempt fails, the status stays `failed`, `/start` returns 503 "warm-up failed", and the page shows "Pundit unavailable" to every visitor.
  - **Nothing restarts the container.** `/healthz` still returns 200, so the liveness probe passes.
  - **Failure is plausible.** The model call has a 15 s budget (`live_timeout_seconds`). Measured warm-ups have reached 11 s for the model call alone (DECISIONS:176), plus a cold managed-identity token.
  - **Restarts will happen.** Container Apps will restart or move the replica at least once in the 23 days to 10 Nov. If that restart meets a slow Foundry call or MSI endpoint, the app is down for every judge until someone notices.
  - **Impact:** this is the worst-looking failure available, and the most likely one over three weeks.
- **Fix:**
  - Retry the warm-up in the background with backoff (for example 5 s, 15 s, 30 s, then every 60 s) until it succeeds.
  - While the status is `failed`, let `/start` take the limited path: the cached pipeline, the same as `guard.tripped`. A judge then always gets a match.
  - Add a test: the warm-up fails once, then succeeds, and `/healthz` reaches `ready`.

### 2. Colour lines block the event loop for 0.6–2.4 s each time
- **Where:** `api.py:717-736`. The code calls `build_colour_candidate` and `validate_colour_line` directly inside the `colour_lines` coroutine.
- **Why it matters:**
  - **Measured locally:** 0.6–1.5 s per build across a match in m01 and m03, and up to 2.4 s at m01's 60' mark. The container has 0.5 vCPU, so expect longer.
  - **It reverses earlier work.** The dispatcher was moved to `to_thread` to cut stalls from 814 ms to 86 ms (DECISIONS:476-484).
  - **What freezes:** while a build runs, every replay's SSE stream, audio downloads, questions and probes wait. That happens up to about 5 times per replay per match, and with several viewers the stalls add up.
  - **What viewers notice:** events and lines arrive in bursts, and answers come back late.
  - **No test catches it.** `tests/test_robustness.py` measures the dispatcher, not the colour worker.
- **Fix:**
  - Run `build_colour_candidate(...)` and `validate_colour_line(...)` in `await asyncio.to_thread(...)`.
  - Then check `session.colour_gate.candidate_is_current(generation)` again before publishing. `publish_outcome` already does this.
  - Extend the event-loop test to run with the colour worker on.

### 3. No Azure budget exists
- **Where:** `infra/main.bicep` has no `Microsoft.Consumption/budgets` resource, and `az consumption budget list` returns nothing for the Hackathon subscription.
- **Why it matters:**
  - **The spec requires one.** Build prompt line 86 asks for £75 with alerts at 50%, 80% and 100%.
  - **The in-app caps aren't enough on their own.** They live in memory and reset whenever the container restarts.
  - **No other backstop.** Without a budget, nothing warns you about unexpected spend between now and 10 Nov.
- **Fix:**
  - Create the budget now: billing is already in GBP, so £75 a month with alerts at 50%, 80% and 100% to your email. Add it to Bicep, or create it once with `az consumption budget create` and record that in DECISIONS.
  - The budget resets on 1 Nov, so also add a forecast alert at about £60 for October. October and the first 10 days of November then stay inside £75 combined.

### 4. No architecture diagram
- **Where:** `docs/ARCHITECTURE.md` doesn't exist. Build prompt lines 202 and 304 require it, with a mermaid diagram.
- **Why it matters:**
  - **Judging weight.** Use of Microsoft technology is part of the judging, and a judge needs one picture: Foundry, the Agent Framework Workflow, Speech, Container Apps, the managed identity and App Insights.
  - **The video needs it too.** In under 2 minutes, an architecture slide is the quickest way to show those services.
- **Fix:** write a one-page `docs/ARCHITECTURE.md` with a mermaid diagram covering:
  - **The three routes:** live window, question and full time.
  - **The two live lanes:** the must-say fast path and the tactical lane.
  - **The evidence check, as a gate** before anything reaches the viewer.
  - **The broadcast delay.**
  - **The Azure resources and their roles.**

  Reuse the diagram in the video and the README.

---

## Should fix before submitting

### 5. The README describes Phase 3 and would mislead a judge
- **Where:**
  - `README.md:7`: "Azure deployment and infrastructure remain later phases."
  - `README.md:63`: "The azd and Bicep files are still scaffolding only."
  - `README.md:54`: a local `--env-file` run is the only way given to start the app.
- **Why it matters:** the README is the first thing a judge reads on GitHub. As it stands, the project looks unfinished and not deployed.
- **Fix.** Rewrite it, in this order:
  1. **Opening.** A one-paragraph pitch, the **live URL**, the video link, and "All clubs, players and stadiums are fictional."
  2. **How to try it (60 seconds):**
     - pick m02 at 20x, press Start, and allow audio;
     - press **Why?** on a line;
     - type "What changed for Port Calder after the red card?";
     - try the mic;
     - wait for the recap.
     - Say that a hidden tab pauses the match, and that the 10 s broadcast delay is on purpose.
  3. **Architecture.** The diagram from item 4, and the explainability promise: every number is checked against tool facts and every cited event is at or before the viewer's position.
  4. **Microsoft technology used:**
     - **Microsoft Foundry:** `gpt-5.4-mini`, DataZoneStandard, Australia East.
     - **Microsoft Agent Framework:** `FoundryChatClient`, a `WorkflowBuilder` graph, function middleware for tool capture, and structured output.
     - **Azure Speech:** the MAI voice `en-GB-Harry` with Ryan as fallback, server-side STT and TTS.
     - **Azure Container Apps, Container Registry** (remote build) and **a user-assigned managed identity.**
     - **Application Insights and Log Analytics** with OpenTelemetry `gen_ai` spans.
     - **azd and Bicep, GitHub Actions and GitHub Copilot.**
     - Be accurate about which assistants built what. The repo contains `review-*-opus.md`, so don't imply that Copilot alone wrote it.
  5. **The keyless story, in five bullets:**
     - one user-assigned identity, with roles assigned by GUID;
     - `disableLocalAuth` on Speech, on Foundry and on App Insights ingestion;
     - the registry's admin user off;
     - no secrets in the container app;
     - the browser never gets a token, because STT and TTS run on the server.
  6. **Deploy it yourself:**
     - Prerequisites: an existing resource group `rg-<env>`, an existing Foundry account with a project and a `gpt-5.4-mini` deployment, and an existing Speech resource in a MAI-voice region.
     - The azd settings: the item 6 list, with defaults.
     - `azd up`.
  7. **Guardrails:**
     - the daily caps and what limited mode looks like;
     - 6 replays in total and 3 per address;
     - 6 questions a minute;
     - the kill switch, `azd env set NARRATIVE_MODE off`, then `azd provision`.
  8. **Results, limitations and attribution:**
     - Results: the gate tables from DECISIONS and the trace screenshot (item 9).
     - Limitations:
       - the evidence check grounds numbers, scores and formations, and isn't semantic entailment;
       - state lives in memory, so a restart ends replays.
     - Attribution: bbenz `grounding.py`, MIT.

### 6. `.env.example` and the azd settings don't let anyone else deploy
- **Where:** `.env.example:21` says "leave both values empty to create a new account". The create path was never built (DECISIONS:608), and `infra/main.bicep:15-25` makes those names required.
- **What's missing:**
  - `.env.example` leaves out the required `FOUNDRY_PROJECT_NAME` and `EXISTING_SPEECH_ACCOUNT_NAME`.
  - It also leaves out the optional `EXISTING_SPEECH_RESOURCE_GROUP`, `NARRATIVE_MODE`, `MIN_REPLICAS`, `MODEL_CALLS_PER_DAY` and `TTS_CHARACTERS_PER_DAY`.
  - `azure.yaml`'s preprovision hook fails unless `rg-<env>` already exists.
- **Why it matters:** the spec's Phase 5 gate is "fresh clone, then `azd up` works from the README alone". At the moment it can't.
- **Fix:**
  - Correct the comment at line 21.
  - List every azd setting, with a note on which are `azd env set` values and which are local `.env` values.
  - Say in the README that the resource group must exist first.

### 7. Limited mode turns the voice off, and cached Q&A barely answers
- **Where:**
  - `api.py:1036-1037`: `audio_enabled=not limited`.
  - The deployed cap is `MODEL_CALLS_PER_DAY=450`. That's about 13 live matches a UTC day at the measured 34–36 calls per match (DECISIONS:694-696).
  - Each `data/cached/m0x.json` holds only 1 or 2 cached questions.
- **Why it matters:**
  - **Who trips the cap:** the URL will be public on the submission page. Visitors who aren't judges can use up the model cap by mid-afternoon UTC.
  - **What later judges get:**
    - "pre-written commentary with audio off" and a disabled mic;
    - an answer of "Live answers are unavailable; showing verified facts only" to nearly every question.
  - **It switches off the product's strongest feature, the voice,** even though only the model cap has tripped.
  - **It departs from the spec,** which says "TTS still runs on cached text" (build prompt line 244).
- **Fix:**
  - **Split the two caps:**
    - The model cap sends new replays to cached text with audio still on.
    - Only the TTS cap turns audio off.
    - The notice should name the cap that tripped.
  - **Expand the cached Q&A:** add 5–8 cached questions per match, the evaluation set the spec asks for. Choose them by viewer clock, so that each one passes the evidence check at plausible positions.
  - **Keep 450** (see Cost).

### 8. Two judges on one network share the question limit
- **Where:**
  - `api.py:886`: `limiter.check(_client_ip(request))`, 6 questions a minute per address.
  - `api.py:54`: at most 3 running replays per address.
- **Why it matters:**
  - **Shared limits:** judges on a corporate network or VPN often share one egress address. Two judges asking questions in the same minute share 6, and a fourth judge in the office gets a 429 on Start.
  - **The message is clear,** but it reads as a limit in the product, not as judges being too busy.
  - **The global cap and the spending guard already bound cost,** so these per-address limits are now stricter than they need to be.
- **Fix:**
  - Rate-limit questions per replay token (6 a minute), with a looser per-address ceiling such as 20 a minute.
  - Raise `MAX_REPLAYS_PER_ADDRESS` to 4 or 5. Eviction of absent viewers already protects the global 6.

### 9. Missing evidence that a judge would look for
- **Where:**
  - **Required by the spec but missing:** `docs/` has no trace screenshot and no evaluation results, and there is no `evaluation/` folder (lines 293 and 312).
  - **Also missing:** `observability/queries.kql` and `agents/pricing.py`, so the UI shows no cost per answer (DECISIONS:632).
- **Why it matters:** "explainable" and "observable" are central to the pitch, and a screenshot proves both in seconds.
- **Fix, in this order:**
  1. **App Insights screenshot.** Take one of a single question's operation, `pundit.turn` → `invoke_agent` → `chat` → TTS, which DECISIONS:719 already describes. Save it to `docs/` and link it from the README.
  2. **Results page.** Write `docs/RESULTS.md` from the existing gate tables: model-written answer rate, evidence pass rate, latency, and agent share of lines.
  3. **What wasn't built.** In the README, state plainly that pricing, the KQL file and the evaluation runner were cut. That beats leaving the spec's promises unexplained.

### 10. Commentary lines and the recap never pass the real-name filter, and the recap skips the banned phrases
- **Where:**
  - `viewer_output_block_reason` runs only on Q&A answers (`api.py:204`).
  - `banned_phrase_hits` runs only on live model lines (`pipeline.py:1761`, `2013`).
  - `produce_recap` (`pipeline.py:2514` onwards) checks the headline and the evidence, nothing else.
- **Why it matters:**
  - **The recap is the full-time centrepiece, and it's spoken.**
  - **Allowed words that shouldn't be:** "incredible", "the crowd", "this afternoon" and "injury" are all allowed in it, and all are invented detail.
  - **Low but uncovered risk:** a real name is unlikely, because the model sees only fictional data, but nothing would catch one.
- **Fix:**
  - Run `banned_phrase_hits` (without the "live only" group) and `viewer_output_block_reason` over the recap's headline, summary and `audio_script`, inside the existing retry loop: one retry with feedback, then the deterministic recap.
  - Add `viewer_output_block_reason` to the live wording step next to `banned_phrase_hits`.

### 11. Insights reach the browser before their event
- **Where:** `api.py:499-509`. The `insight` message goes into the outbox **before** the wait for the viewer's release at `api.py:515-518`.
- **Why it matters:**
  - **What leaks:** the page ignores `insight`, but the SSE stream still carries it up to the 10 s broadcast delay early. The payload includes its headline and event IDs, for example a goal's.
  - **Who sees it:** a technical judge looking at the network tab sees a spoiler.
  - **The rule it breaks:** "never ahead of the viewer".
- **Fix:** move the insight `put` to after the `release_condition` wait, or stop sending it, since nothing uses it.

### 12. Real club and player names in the repo
- **Where:**
  - `docs/DECISIONS.md:391` and `docs/reviews/review-2-opus.md:33`: a real club, in the test question about pressing.
  - `docs/reviews/review-2-opus.md:320-321`: a real player and a real club.
- **Why it matters:** non-negotiable 5 says no real club or player appears **anywhere**, and the repo is already public.
- **Fix:**
  - Replace them with "[a real club]" or "[a real player]". The test itself already uses hashed fingerprints.
  - History still holds the old text. Rewriting history isn't worth it for docs that quote the guardrail working.

### 13. Stale or contradictory docs
- **`docs/DECISIONS.md`:**
  - **Line 1** is titled "# Phase 0 decisions" but covers the whole project. Retitle it "Decisions" and add a short contents list.
  - **Line 723** says Speech still has key auth enabled and that Foundry doesn't report the flag. Both are now `disableLocalAuth: true`, as verified today. Update it.
  - **Lines 648 and 700** give the model-call cap as 900; the deployed value is 450. Record the change and why.
- **`prompts/build-prompt-v2.md`:**
  - **Line 19** (changelog) still says "at most 3… a replay stops when its SSE client disconnects, with no resume", and **line 316** lists resume as out of scope. Line 143 already says resume. Add a v2.1 changelog line noting that Phase 4 changed both.
- **The three commentary issues you listed** aren't actually written down in DECISIONS.md. Add them under the polish session, with the verdicts below.
- **`docs/screenshots/app-shell/`** predates the mobile session and the crowd bed. DECISIONS:522 has the crowd toggle marked "soon". Either retake the screenshots or label the folder as historical.

### 14. Leftover resources with key auth on
- **Where:** `rg-pundit` holds three Cognitive Services accounts the app doesn't use, all with local auth enabled and public network access:
  - `second-screen-pundit-resource` (UK South)
  - `second-screen-pundits-resource` (Sweden Central)
  - `pundit-speech-kl` (UK South)

  They have no model deployments, so they cost nothing.
- **Why it matters:** they aren't in the repo or the app, but "key auth disabled everywhere" isn't true of the resource group a judge might hear about.
- **Fix:** delete them, or set `disableLocalAuth`.

### 15. Repeated score after a goal (known issue)
- **Where:**
  - `dispatcher.py:273`: the tactical lane takes its `recent_lines` snapshot at dispatch.
  - `dispatcher.py:316`: lines are appended only at publish.
- **Why it matters:**
  - **How it happens:** a tactical window dispatched before the goal line is published doesn't know the score was just said, so both lines state it seconds apart.
  - **Why it shows:** it happens right after a goal, the moment a judge or the video is most likely to be listening.
  - **Verdict:** it matters a little for judging.
- **Fix:**
  - At publish, if a line states the score and either of the last 2 published lines stated the same score, apply `strip_withheld_score` (`wording.py:444`) before publishing.
  - For the video, choose a take where this doesn't happen.

### 16. "The away side" opener (known issue)
- **Where:** `wording.py:226`. `BANNED_OPENERS` covers "the home side" and "the visitors" but not "the away side".
- **Why it matters:**
  - **Generic and against house style:** it's the kind of line the full-name-first rule exists to stop.
  - **Verdict:** it matters a little, because judges hear generic phrasing as AI filler.
- **Fix:** add "the away side" to `BANNED_OPENERS`, or rewrite it to the team's full name in `apply_house_style`, which avoids a fallback.

---

## Fine as it is

### Correctness and spoilers
- **Q&A cut-off.**
  - `answer()` reads `session.viewer_released_as_of` once (`api.py:1114`), and every tool, snapshot and check uses that value.
  - The client sends no minute, and `check_question_access` refuses a question before the first released event.
  - Cached answers are re-checked at that position (`pipeline.py:2465-2512`). An answer citing a later event falls back to facts, so no cached spoiler gets through.
- **Lines and colour lines.**
  - A line waits for its event's release (`api.py:515-518`).
  - Colour lines are built at the viewer's released position and are blocked while a must-say event is still inside the delay.
- **Replay token.** It is 192-bit, compared in constant time, and kept only in page memory, and a missing token gets the same 404 as a missing replay.
- **Unauthenticated lookups.** `/api/lines/{id}/evidence` and `/api/audio/{id}` need no token. Their IDs are UUID-derived and the data is fictional.
- **Eviction.** Only paused replays with no stream are evicted, the longest-gone first, and a viewer who is present is never evicted.
- **Resume.** It works across both the planned 200 s close and an unplanned drop, and the tests in `test_stream_resume.py` and `test_away.py` include mutation checks.
- **Wrong-team claims on goal lines.**
  - These are caught only for the listed scorer phrases (`wording.py:320`). A line such as "Port Calder find the net" credited to the wrong side would pass.
  - No gate run produced one, and the score in a goal line is checked against the score fact.
  - Leave it, but name it in the README's limitations: the check grounds numbers, scores and formations, not every claim.

### Known commentary issue: logging rejected scorer-phrase lines
- **Where:** `pipeline.py:1776` logs `wording_rejected` with the failure reason but not the rejected text.
- **Verdict:** this is a diagnostics gap, and judges never see it. It doesn't matter for judging, so leave it until after submission.

### Demo risks that are handled
- **The first visit and a cold start.**
  - With `minReplicas` 1 there is no cold start.
  - After a restart, the page shows "Warming up the pundit… Ns", and errors during start-up aren't shown in red.
  - Item 1 is the only gap.
- **Busy.** A friendly card with **Try again** appears, nothing shows in red, and absent viewers are evicted first.
- **Phone lock.** The match pauses, a Welcome back card offers a tap to resume, the replay is held for 10 minutes, then "This replay has ended" offers Start again. The deployed app passed the iPhone checks (DECISIONS:917).
- **Tab switch on a desktop pauses the match.** This is by design, and the Welcome back card explains it.

### Security and keyless
- **The repo and its history.** The scan of tracked files and all 75 commits of `git log -p --all` found no keys, connection strings, SAS tokens, private keys or token prefixes. No `.env`, `.azure/`, `.pem` or `.pfx` has ever been committed, and `.env`, `.azure/`, `.claude/` and `.playwright-mcp/` are ignored.
- **The image.** `.dockerignore` excludes `.env`, `.azure`, `.git`, docs, tests and infra. It runs as non-root uid 10001, and DECISIONS:721 records the image scan.
- **Deployed settings.**
  - `secrets` is null, and the registry entry has an identity and no password.
  - The only key-shaped value is the App Insights connection string. It identifies the resource and can't send telemetry, because `DisableLocalAuth` is true.
- **Key auth on the resources the app uses.** `pundit-speech-swc-kl` and `second-pundit-resource` both have `disableLocalAuth: true`, verified today.
- **Public surface.**
  - Every replay call needs the token.
  - Questions are length-limited, the uploaded audio is capped and checked as WAV, and the client address is the right-most `X-Forwarded-For` entry.
  - The public Swagger UI at `/docs` shows only that already-protected API, so leave it on. Judges may like it.

### Rules
- **Fictional names.**
  - The squads are curated and invented, and a test checks club names against a hashed denylist.
  - The footer says "All fixtures and players are fictional."
  - Out-of-scope questions get a fixed reply with no model call.
- **No real commentator imitation.** `prompts/commentator-persona.md:13` forbids naming, imitating or quoting one, and the voice is a stock MAI voice.
- **No betting.** The Q&A filter and the live banned list cover it.
- **No copyrighted audio.** The crowd bed and whistles are generated in the browser. Keep the video free of copyrighted music too.
- **The hackathon name.** It appears in the GitHub description and in `prompts/build-prompt-v2.md:58`. That's the event's own name, and the build prompt is a fair record of how the app was built.
- **Official rules** ([OFFICIAL RULES.md](https://github.com/microsoft/insidethegamehackathon/blob/main/OFFICIAL%20RULES.md), read after the first draft of this review). Already compliant:
  - Synthetic data only (§4.7).
  - A new project (§4.3): the first commit is 16 Oct; registration opened 29 Sep.
  - The repo is public (§4.4), and everything is in English (§4.9).
  - Open source used under its licence and built on (§4.11): the bbenz MIT adaptation keeps its licence.

  The rest is under "Official rules" at the end.

### Cost against £75 to 10 Nov
- **Spend so far and the window.** Billing is in GBP. Month-to-date actual cost is **£4.45**: Foundry Models £3.74, Foundry Tools £0.63, ACR £0.08. Container Apps hasn't been billed yet. From today to 10 Nov is 23 days.
- **Fixed costs** with `minReplicas` 1: an idle replica, ACR Basic and Log Analytics come to about $16, roughly **£12**, after the free grant. That uses DECISIONS:731-734 prorated.
- **Worst-case compute.** If a tab keeps a stream open around the clock, the replica bills as active. Hidden tabs close their stream, so this needs a visible tab left open: about $34, or **£26**, for the period.
- **The usage ceiling from the deployed caps.**
  - The 450 model calls cost about $1.14 a day; the 45,000 TTS characters cost at most $0.99 a day.
  - Over 23 days that is about $49, or **£37**.
- **Worst case in total:** about **£70–75** (exchange rate assumed at about £0.76 to the dollar). That only happens if the caps are hit every day and a tab stays open for weeks.
- **Realistic case:** **£15–25.**
- **Don't raise the model cap back to 900.** It adds about £20 to the worst case and goes over £75. If more model headroom is wanted, 600 is the most that still fits.
- **`maxReplicas` 1** is required by the in-memory state.
- **`minReplicas` 1** is worth its roughly £10 for judging.
- **After 10 Nov,** run `azd env set MIN_REPLICAS 0` and `azd provision`, or delete the app.

---

## Official rules (added after the first draft)

These come from the official rules. They change the lists above.

- **Must, before 20 Oct, 12:00 PT (20:00 UK): register** on the Hackathon Website (§1, §4). Registration closes a week before submission.
- **Must, before the video: personalisation is missing.**
  - §4.1(i) defines one pipeline with five stages, and the fifth is **Personalize**: "the same intelligence reaches a data-hungry analyst and a casual fan as two genuinely different experiences". It suggests favourite club, favourite player, a player-focus mode or language.
  - It adds that "a solution that only personalizes has nothing to personalize". The reverse holds too: an app that only generates metrics has no audience.
  - The app has none of this. It bears on "Adherence to Hackathon Category" (20%) and on Stage One, the pass/fail check that the project "reasonably fits the theme".
  - **A light version is enough:**
    - **A Casual / Analyst toggle.** Casual hides numbers in the stats panel, plays fewer colour lines and asks the Explainer for plain answers. Analyst shows the evidence view open by default, xG and possession.
    - **A "My team" pick.** It frames lines and answers from that side, for example "your side", and gives that team's goals the higher-energy tier.
  - Both work on the server's existing facts, so the evidence check is unchanged.
  - This is a scope change from the build prompt, so it's your decision.
- **Should: say why there's no multi-language output.** §4.1(vi) lists it among the challenge areas, and the build prompt puts it out of scope. Say so in the README. If there's time, an `xml:lang` voice switch on the recap alone would show it.
- **Should: keep the URL up until 10 Nov, 23:59 PT** (§1; that's 07:59 on 11 Nov UK time). The access must be "free of charge and without any restriction" (§4.8). The daily caps degrade the app rather than block it, which I'd consider fine. The per-address limits (item 8) are the nearest thing to a restriction.
- **Should: no third-party trademarks in the video** (§4.4). Leave the league's name out of the video, and out of its title to be safe. That's stricter than the build prompt's allowance.
- **Should: the pitch must name the Microsoft and Azure technology used** (§4.4). Item 5's list covers it.
- **Optional: MCP.** The judging criteria name Azure MCP and "MCP integration" among the hero technologies. Exposing the read-only analytics tools as an MCP server, for Copilot or another agent to query a match, would score on both technological implementation and agentic design.

## Order of work

| # | Item | Effort |
| --: | :-- | :-- |
| 0 | Register on the Hackathon Website (rules, by 20 Oct, 12:00 PT) | 5 minutes |
| 0b | Decide on personalisation (rules): a Casual/Analyst toggle and "My team" | about a day if yes |
| 1 | Warm-up retry, and cached Start when it failed (1) | about 1 hour with a test |
| 2 | Colour build off the event loop (2) | 15 minutes, plus the loop test |
| 3 | Azure budget (3) | 10 minutes |
| 4 | `ARCHITECTURE.md` and its diagram (4), then record the video | half a day |
| 5 | README rewrite, `.env.example`, trace screenshot and results (5, 6, 9) | half a day |
| 6 | Limited mode keeps audio, more cached Q&A, and the per-token question limit (7, 8) | 2–3 hours |
| 7 | Recap and line filters, the insight leak, and the score repeat and opener (10, 11, 15, 16) | 2 hours |
| 8 | Doc tidy, real names and leftover resources (12, 13, 14) | 1 hour |
