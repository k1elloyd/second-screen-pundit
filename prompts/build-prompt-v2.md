# Build Prompt v2: Second Screen Pundit

*Generation prompt for a coding agent (GitHub Copilot agent mode in VS Code). Modelled on Brian Benz's `prompts/demopromptv1.md` in [bbenz/football-results-dashboard](https://github.com/bbenz/football-results-dashboard) (MIT). Run **one phase per chat session**, so each session stays small and the agent stops at every gate.*

---

## Changelog (v1 → v2)

- **Models (A1):** `MODEL_FAST` and `MODEL_DEEP` now hold deployment names. Both point at one `gpt-5.4-mini` deployment. All Sol references removed. Bicep deploys one model deployment unless `MODEL_DEEP_MODEL` is set.
- **Agent Framework options (A2):** `max_output_tokens` → `max_tokens`. `store` and `response_format` are set through `default_options`, and results are read from `response.value`.
- **Reasoning state (A3):** Phase 0 now checks whether tool loops with `store: False` need `reasoning.encrypted_content`.
- **Orchestration (A4):** group chat or handoff replaced by an Agent Framework Workflow, with one run per window or question. Tool results are captured by function middleware.
- **Tracing (A5):** the agent no longer creates GenAI spans by hand. Agent Framework emits them. The `app.*` attributes move to a parent `pundit.turn` span.
- **Speech (A6/B5):** no Speech tokens go to the browser and `/api/speech-token` is removed. The browser records push-to-talk audio and posts it to the new `/api/ask-audio`. The server does STT and TTS with the Python Speech SDK and the app's single managed identity, and serves audio as short files from the new `/api/audio/{audio_id}`. Non-negotiable 6 is rewritten to match.
- **RBAC (A7):** roles are assigned by role definition GUID. Foundry User is the role for model calls.
- **Packages (A8):** exact Agent Framework versions are pinned. Samples using pre-2026 API names are to be ignored.
- **Cooldown (B1):** now measured in match time (default 4 match-minutes), with must-say events exempt. A pipeline still running when the next window arrives makes the older window drop instead of queueing. Each run captures its as-of point when dispatched.
- **No future data (B2):** every tool is cut off at a server-injected `as_of`. `/api/ask` no longer takes a client `minute`. The cut-off is the event `seq`, so stoppage time can't make it ambiguous.
- **Replays (B3):** one replay per browser tab, at most 3 running at once. A replay stops when its SSE client disconnects, with no resume. The stream path is now `/api/replays/{replay_id}/stream`.
- **Cached mode (B4):** outputs are committed under `data/cached/` (exempt from `.gitignore`) and evidence-checked by a test. Cached Q&A behaviour is defined.
- **Branding (B6):** the hackathon name may appear in the README and the video title only.
- **Evidence endpoint (B8):** `CommentaryLine` gains a `line_id`.
- **Pipeline (C1):** the live, question and full-time routes are spelled out. The Commentator calls no tools. The Recap Producer may call up to 4.
- **Number check (C2):** scope defined: digits, number words zero to ten, and scores and formations, which must match cited facts. Shirt numbers are excluded.
- **Event schema (C3):** `player_id` optional, `added_minute` for stoppage time, goals as separate events, own goals, per-team normalised coordinates, `end_x`/`end_y` for passes and carries, receiver for passes, shot outcome meaning.
- **Metrics (C4):** definitions added for possession, pressing intensity, high turnover, progressive pass, `compare_windows` parameters and `key_moments` ranking.
- **Storylines (C5):** beats are injected at fixed minutes. In m02 the red card goes to the home team.
- **Container Apps (C6):** `maxReplicas: 1`, SSE heartbeats every 15 seconds, check the ingress idle timeout, README note on cold start.
- **Time budgets (C7):** per-agent budgets, including the retry, with defined behaviour on timeout.
- **Budget resource (C8):** billing currency, contact email parameter, first-of-month start date. `NARRATIVE_MODE=off` documented as the kill switch.
- **CI (C9):** CI runs tests only, and live tests are skipped. No GitHub Actions deploy; deployment is a manual `azd up`.
- **Fictional names (C10):** curated invented name lists plus a denylist test for club names.
- **Out of scope:** added deployment from CI and resuming a replay after disconnect.
- **Existing Foundry account (B7):** Bicep takes `existingFoundryAccountName` and `existingFoundryResourceGroup`. When they are set, it references the existing account and its `gpt-5.4-mini` deployment, creates no Foundry account or deployments, and assigns the app identity Foundry User on that account by role GUID. This is the default path in `.env.example`.
- **Replay controls:** speed is set only at `/start`. Added `POST /api/replays/{replay_id}/control` with `pause` and `resume`. Seek removed from the spec.
- **Housekeeping:** deleted the stale "copy this file into the new repo" sentence and the `[[…]]` link. Deleted `prompts/build-prompt-v1.md`.
- **Phase 3 viewer cut-off:** typed and spoken viewer questions and answers use only events through the viewer's released position, never a newer agent `as_of`.

---

## Non-negotiables

Read these first. Everything else elaborates on them.

1. **Build only what this prompt describes.** It is the requirements document. Don't add features, pages or services it doesn't mention. If something seems missing, ask.
2. **Ship running code, not plans.** Never claim a command, deployment, model call or test passed without showing the output that proves it.
3. **Check before you design around something.** Verify Microsoft Agent Framework, Microsoft Foundry, Azure Speech, Azure Container Apps, azd and Bicep behaviour against current Microsoft Learn documentation before relying on it. Don't assume a preview feature, SKU, package name or API version exists. If the docs and this prompt disagree, stop and tell me.
4. **Deterministic code owns every number and every fact.** Analytics tools compute all statistics from the match event log. Models choose tools, decide what matters and explain results. They never compute, invent or override a statistic. Every number and every event a model output mentions must trace back to a tool result, and an automated check must prove it.
5. **All match data is synthetic.** Use fictional clubs, players and stadiums only. No real club, player, league, badge, crest or trademark appears anywhere: not in code, data, the UI, tests or the video. The one exception is the hackathon's own name, which may appear in the README and the video title. No match footage.
6. **Use keyless access everywhere.** Use one user-assigned managed identity in Azure and `DefaultAzureCredential` locally. Never write, log, print or commit keys, connection strings or tokens. The browser never receives any key or token: all speech-to-text and text-to-speech run on the server.
7. **Cost guardrails are features, not extras.** Every model call has `max_tokens`, a time budget and `store: False`. The public API is rate-limited per client. At most 3 replays run at once. Container Apps scale to zero.
8. **Keep it one container.** A single Python API serves the static front end, the event stream, the audio and the agents. Don't split it into microservices.
9. **Stop at each phase gate.** Report what was built, the evidence that it works (command output, test results) and anything you are unsure about. Then wait for me. Don't start the next phase on your own.
10. **Make routine engineering choices yourself.** Don't ask permission to write code, add a test or pick a reasonable library version.

## Mission

Build **Second Screen Pundit** for the Microsoft x Premier League *Inside the Game* developer hackathon (submission deadline 27 Oct 2026). The brief is: AI-powered solutions that turn synthetic, football-realistic data into explainable match intelligence, real-time narratives and automated match recaps for studio, broadcast or streaming use.

The app replays a synthetic football match as a live event stream. A team of agents watches it, decides what's worth saying, and produces short spoken commentary. Viewers can ask spoken or typed "why" questions and get answers that cite the exact events behind them. At full time the app produces a written recap and an audio recap. **Every claim links to event IDs, so the commentary is explainable rather than just plausible.**

Judging gives equal weight to technological implementation, agentic design and innovation, real-world impact, UX and presentation, and fit with the hackathon category. Target prizes: Best Multi-Agent, then Best Use of Microsoft Foundry.

## Stack

- Python 3.12, FastAPI, uvicorn, pydantic v2, pytest.
- **Microsoft Agent Framework** (Python) with `FoundryChatClient` against a Microsoft Foundry project. The package is `agent-framework-foundry`, imported as `from agent_framework.foundry import FoundryChatClient`; orchestration types live in `agent_framework.orchestrations`. Verify the current install command on Learn and **pin exact versions** in `pyproject`. The Python API changed in 2026: ignore any sample that uses `ChatAgent`, `AzureAIAgentClient` or `model_id`.
- **Model calls:** set `max_tokens`, `store: False` and `response_format` (a pydantic model) through the agent's `default_options`, and read the parsed result from `response.value`. `FoundryChatClient(model=...)` takes the **deployment name**.
- **Two model settings**, set by configuration and never hard-coded. `MODEL_FAST` (Director, Match Analyst, Commentator) and `MODEL_DEEP` (Tactics Explainer, Recap Producer) hold **deployment names**, not model names. *Current setup: both point at one deployment of `gpt-5.4-mini` (DataZoneStandard, Foundry resource in Australia East). Don't reference any Sol model unless I add it.*
- **Azure Speech**, used from the server only through the Python Speech SDK (`azure-cognitiveservices-speech`) with the app's managed identity:
  - Neural TTS for commentary lines, spoken answers and the audio recap, written as short MP3 files.
  - STT for push-to-talk questions.
  - Entra authentication needs a **custom subdomain** on the Speech resource and the *Cognitive Services Speech User* role. Verify the current Python Entra auth pattern on Learn, and refresh the token before it expires.
- **Front end:** one HTML page with vanilla JS and CSS, no build step, served by FastAPI. It has a live timeline, a pitch mini-map, a commentary transcript, a "why?" button on each line, an ask box with a push-to-talk mic button, and a recap tab.
  - The mic button records audio in the browser, encodes it as **16 kHz mono 16-bit PCM WAV** (via the Web Audio API, so the server needs no GStreamer) and posts it to `/api/ask-audio`.
  - The page plays commentary and answers from `/api/audio/{audio_id}`.
  - The mini-map flips the away team's coordinates for display (see the event schema).
- **OpenTelemetry** to Azure Monitor / App Insights, using `gen_ai.*` semantic conventions (see Observability).
- **Infrastructure:** `azd` with Bicep. It provisions:
  - Azure Container Apps (consumption, `minReplicas: 0`, `maxReplicas: 1`)
  - Azure Container Registry
  - **one** user-assigned managed identity
  - Foundry: see the next bullet. On the create path only, a Foundry account with a project and **one** model deployment (`versionUpgradeOption: NoAutoUpgrade`); a second deployment only if `MODEL_DEEP_MODEL` is set
  - Speech (with `customSubDomainName` and `disableLocalAuth: true`)
  - Log Analytics and App Insights
  - a **budget resource (£75 per month, with alerts at 50%, 80% and 100%)**
- **Existing Foundry account (default path):** Bicep takes `existingFoundryAccountName` and `existingFoundryResourceGroup`.
  - When both are set, Bicep references the existing account and its `gpt-5.4-mini` deployment (the deployment name comes from `MODEL_FAST`) as `existing` resources, and creates **no** Foundry account or deployments. The account is in the same subscription.
  - It assigns the app identity *Foundry User* on that account by role GUID, through a module scoped to `existingFoundryResourceGroup`.
  - The app reads the project endpoint from `FOUNDRY_PROJECT_ENDPOINT` in the azd environment.
  - When both are empty, Bicep creates the Foundry resources listed above.
  - `.env.example` sets `EXISTING_FOUNDRY_ACCOUNT_NAME` and `EXISTING_FOUNDRY_RESOURCE_GROUP` (mapped to the Bicep parameters in `main.parameters.json`) as the default, with placeholder values and a comment explaining that leaving them empty selects the create path. This avoids new deployments competing for the same subscription quota.
- **Role assignments:** use role definition GUIDs in Bicep, not role names, because the Foundry roles were recently renamed. The managed identity gets *Foundry User* (`53ca6127-db72-4b80-b1b0-d745d6d5456d`) on the Foundry account and *Cognitive Services Speech User* on the Speech resource (look up its GUID on Learn).
- **Budget details:**
  - `Microsoft.Consumption/budgets` uses the billing account's currency, so the amount is £75 only if billing is in GBP. Check this and tell me if it isn't.
  - The budget takes a contact email as a Bicep parameter, and its start date must be the first of a month.
  - Budget alerts only notify; they don't stop spending. The kill switch is `NARRATIVE_MODE=off`. Bicep passes it from the azd environment to the container, so `azd env set NARRATIVE_MODE off` followed by `azd up` turns off all model calls. Document this in the README.
- **No local Docker.** Build images remotely with Azure Container Registry (`docker: remoteBuild: true` in `azure.yaml`). Don't require Docker Desktop on the developer machine.
- **GitHub Actions:** run tests on every push, and nothing else. There is no deploy workflow; deployment is a manual `azd up`. Tests never call Azure. Mark any test that does with `@pytest.mark.live` and skip those in CI.

## Synthetic match data

### Event schema (`engine/schema.py`)

Each event is a pydantic model:

| Field | Notes |
| :---- | :---- |
| `event_id` | Stable string, `{match_id}-e{seq:04d}`, e.g. `m01-e0042` |
| `match_id`, `seq`, `period` | `seq` is the strict ordering and the cut-off for "what has happened so far". `period` is 1 or 2 |
| `minute`, `added_minute`, `second` | `minute` is the match clock, capped at 45 in period 1 and 90 in period 2. `added_minute` (0 or more) counts stoppage time, so 90+3 is `minute: 90, added_minute: 3`. `second` is 0–59 |
| `team` | `home` or `away`. `null` for `half_time` and `full_time` |
| `player_id` | Optional. References the match's fictional squad. `null` for `kickoff`, `half_time`, `full_time` and `formation_change` |
| `type` | `kickoff`, `pass`, `carry`, `shot`, `goal`, `save`, `tackle`, `interception`, `foul`, `card`, `substitution`, `formation_change`, `half_time`, `full_time` |
| `x`, `y` | Pitch coordinates 0–100, **normalised so the acting team always attacks towards x = 100**, in both halves |
| `end_x`, `end_y` | `pass` and `carry` only: where the ball ended up, same normalisation |
| `outcome` | `success` or `fail`, where relevant. For a `shot`, `success` means on target |
| `xg` | `shot` only, 0–1 |
| `detail` | Type-specific dict, e.g. card colour, sub in/out, new formation, `receiver_id` for a successful pass, `own_goal: true` |
| `related_event_ids` | E.g. the pass that led to a shot, or the shot a goal came from |

Goals and own goals:
- A goal is its own `goal` event that references its `shot` through `related_event_ids`. Shot counts and xG come only from `shot` events, so nothing is counted twice.
- For an own goal, `team` is the team credited with the goal, `player_id` is the player who put the ball in, and `detail.own_goal` is `true`. No `shot` precedes an own goal, and it adds no xG.

### Generator (`engine/generate.py`)

- It is seeded and deterministic: the same seed gives the same match. It produces a realistic event rate (about 1,500–2,000 events per match), possession swings, xG that loosely matches the score, and formation changes after red cards and substitutions.
- It ships three **storyline scripts**. Each is a seed plus scripted beats, stored in `engine/storylines.yaml`. **Beats are injected at fixed minutes** (goals, cards, substitutions, formation changes and the shots behind the goals), and random play fills in around them. Don't search for seeds that happen to produce the story.
  - `m01` **Late comeback**: 0–2 at 70', 3–2 at full time after the home team switches to a back three.
  - `m02` **Red card reshape**: the **home** team gets a 38' red card and switches to a 4-4-1, and the away team camps in the final third.
  - `m03` **Pressing masterclass**: the home team wins on high turnovers. The away team's total xG stays below a threshold set in the script.
- It uses fictional clubs and squads (`engine/squads.yaml`), for example "Ashbridge Rovers" and "Port Calder FC". Names must not resemble real clubs or players:
  - Build player names from **curated, obviously invented** first-name and surname lists in the repo, not from a random-name library.
  - Add a test that checks every club name against a short denylist of real club names.
- It writes to `data/matches/{match_id}.jsonl` and generates at startup if the file is missing. Generated match files are build artefacts, so add them to `.gitignore`.

### Replay (`engine/replay.py`)

- It streams events at a speed from 1x to 120x, set once at `/start`. It can be paused and resumed. There is no seek and no change of speed mid-replay.
- While paused, it publishes no events and starts no new pipeline runs. Runs already in progress finish, SSE heartbeats continue, and viewer questions are still answered.
- It publishes to an in-process async queue that feeds that replay's agents and its server-sent events (SSE) stream.
- Each replay belongs to one browser tab. At most 3 replays run at once (configurable). A replay stops, and its agent runs are cancelled, as soon as its SSE client disconnects. There is no resume: reloading the tab starts a new replay.

## Analytics tools (`agents/tools/`)

Each tool is deterministic and read-only, with typed pydantic parameters (`extra="forbid"`, `Literal` choices). Each returns a `ToolResult`:

```
ToolResult {
  tool: str
  facts: [ { fact_id: str, label: str, value: float|int|str, unit: str|null, event_ids: [str] } ]
  table: optional small list of rows
  caveats: [str]
  cannot_tell: [str]
}
```

Each analytics tool returns its complete summary facts and no more than five distinct evidence event IDs in total. Keep evidence checks grounded in those IDs actually returned.

**No future data:** every tool is bound to the current replay's match and an `as_of` cut-off (the `seq` of the last published event, with its minute for display). The server injects `as_of` when the pipeline run is dispatched. The model never chooses it and it is not a model-visible parameter. Tools only read events with `seq <= as_of`. Any minute parameter later than `as_of` is clamped to it, and the clamp is noted in `caveats`.

The tools to provide are:

| Tool | Returns |
| :---- | :---- |
| `match_state(minute)` | Score, possession %, shots, xG, cards and formations at that minute |
| `possession_window(start, end)` | Possession share per team in a window |
| `xg_timeline()` | Cumulative xG per team per 5-minute bucket |
| `shot_summary(team, start, end)` | Shots, on target, xG, best chance (with its event ID) |
| `pressing_intensity(team, start, end)` | Defensive actions in the opponent's half per minute, and high turnovers |
| `pass_network(team, start, end)` | Top passing pairs and progressive passes |
| `formation_at(team, minute)` | Formation and the event ID of the last change |
| `key_moments(limit)` | Goals, cards, big chances and formation changes, ranked |
| `compare_windows(team, metric, before, after)` | Before and after a moment (e.g. a sub or red card) for one metric |

Metric definitions (all thresholds in config):

- **Possession share:** a team's successful passes divided by all successful passes in the window.
- **Defensive action:** a `tackle`, `interception` or `foul` by the team. **Pressing intensity** counts defensive actions with `x >= 50` (the opponent's half) per minute of the window.
- **High turnover:** a successful `tackle` or `interception` by the team with `x >= 60`.
- **Progressive pass:** a successful `pass` with `end_x - x >= 25`.
- **Passing pair:** a passer and their `detail.receiver_id`, counted over successful passes.
- **Big chance:** a `shot` with `xg > 0.25`.
- **`compare_windows`:** `metric` is a `Literal` of `possession`, `xg`, `shots`, `pressing_intensity`, `high_turnovers` and `progressive_passes`. `before` and `after` are each a minute range `[start, end)`, and both are clamped to `as_of`.
- **`key_moments` ranking:** goals first, then red cards, then formation changes, then big chances by xG (highest first), then yellow cards. Ties go to the earlier event.

Every tool gets unit tests on hand-built mini fixtures with hand-calculated expected values.

## Agents (`agents/`)

Build them with Microsoft Agent Framework. Every agent returns structured JSON validated by pydantic, and every output carries `fact_ids` and `event_ids`.

### Orchestration

Use an Agent Framework **Workflow** (`WorkflowBuilder`) with agents as executors and conditional edges for routing. Start a **fresh workflow run for each window, question or full-time trigger**. Don't keep a long-lived shared conversation.

Don't use group chat or handoff orchestration:
- Group chat sends the full history to every participant and strips tool calls and results from shared history.
- Handoff waits for human input when an agent doesn't hand off.

Capture every tool result in the run with Agent Framework **function middleware** (verify the current API on Learn), not from chat history. The evidence check reads from that capture. Document the workflow graph in `docs/ARCHITECTURE.md`.

The three routes are:

- **Live window:** replay → Director → skip, *or* Match Analyst → Commentator → evidence check → TTS → SSE.
- **Viewer question:** `/api/ask` or `/api/ask-audio` → Tactics Explainer → evidence check → response (and TTS for spoken questions).
- **Full time:** `full_time` event → Recap Producer → evidence check → SSE, with the audio recap written by TTS.

### Agent roles

1. **Director** (`MODEL_FAST`), the orchestrator.
   - **Deterministic pre-filter:** goals, cards, substitutions, formation changes and big chances always go forward. These are the *must-say* events. Otherwise it sends one event window every 3 match-minutes.
   - **Model decision:** for windows that aren't must-say, it decides whether the window is worth commentary (an `importance` score from 0 to 1, with a threshold from config).
   - **Cooldown:** at most one commentary line per 4 match-minutes (configurable). Must-say events are exempt.
   - **Backpressure:** if a window's pipeline run is still in progress when the next window arrives, drop the older window. Never queue windows. A must-say event replaces a pending ordinary window.
   - Each pipeline run captures `as_of` when it is dispatched, and all its tools and checks use that value.
   - **Viewer questions** go to the Tactics Explainer. **Full time** triggers the Recap Producer.
   - For typed or spoken viewer questions, the server's evidence cut-off is the viewer's released event position. It must never use the agents' newer `as_of`.
2. **Match Analyst** (`MODEL_FAST`). It calls the tools and returns an `Insight { insight_id, minute, headline, fact_ids, event_ids, importance }`.
3. **Commentator** (`MODEL_FAST`). It calls **no tools**. It turns an insight into a `CommentaryLine { line_id, text (≤ 200 chars, 1–2 sentences), insight_id, fact_ids, event_ids, tone }`, where `line_id` is `{match_id}-l{seq:03d}`. It has a broadcast persona, British English, no hype words, and is never allowed to state a number that isn't in the insight's facts. It refers to players by name, not shirt number.
4. **Tactics Explainer** (`MODEL_DEEP`). It answers a viewer's question with `Explanation { answer (≤ 80 words), claims: [{ text, fact_ids, event_ids }], cannot_tell }`. It calls tools as needed. If the data can't answer the question, it says so in `cannot_tell` and doesn't guess.
5. **Recap Producer** (`MODEL_DEEP`). At full time it calls tools (within the per-turn limit) and produces `Recap { headline, summary (≤ 200 words), key_moments: [3 × { minute, text, event_ids }], audio_script (≤ 150 words), fact_ids }`.

### Evidence check (`agents/evidence.py`, plain code, no LLM)

Run it on every agent output before it reaches the viewer:

1. **Event check:** every cited `event_id` must exist in the match log with `seq <= as_of` for this run. Commentary can't reference the future.
2. **Fact check:** every `fact_id` must exist in this run's captured tool results. For the Commentator, that means the facts in its insight.
3. **Number check:** this covers digits (including decimals and percentages) and the number words zero to ten. Adapt `agent/grounding.py` from bbenz/football-results-dashboard (MIT) and credit it in the file header and README.
   - Each number must match a fact value from this run's tool results, or the minute of a cited event. Rounding to the precision written passes: 54.6 may be written as 55%.
   - Scores (e.g. "3–2") and formations (e.g. "4-4-1", "a back three") must match a score or formation value in a cited fact.
   - Shirt numbers are excluded: the checker skips numbers written as `No. 9` or `#9`.
4. **If a check fails:** retry once with the failure as feedback. If it fails again, drop the commentary line (live), or return the deterministic facts with a visible notice (Q&A and recap).

### Narrative modes

The **narrative mode** setting is `live | cached | off`.

- **`cached`** is a fallback during judging. It replays labelled pre-generated outputs for the three storyline matches.
  - The outputs are committed as `data/cached/{match_id}.json`. This path is exempt from the `.gitignore` rule for generated data, and a test runs the evidence check over every cached output.
  - In cached mode, `/api/ask` and `/api/ask-audio` answer only the evaluation questions, using the cached answer of the closest-matching question. Any other question gets the deterministic facts and a visible "live answers unavailable" notice.
  - TTS still runs on cached text. If Speech fails, the UI shows text only.
- **`off`** makes no model calls at all.

### Time budgets

All budgets are configurable and include the one retry:

| Run | Budget | On timeout |
| :---- | :---- | :---- |
| Live window (Director + Analyst + Commentator) | 15 s | Drop the line |
| Tactics Explainer | 20 s | Deterministic facts with a notice |
| Recap Producer | 45 s | Deterministic facts with a notice |

STT and TTS time doesn't count against the model budgets. `/api/ask-audio` has an overall limit of 30 seconds.

## API (`api/`)

| Endpoint | Purpose |
| :---- | :---- |
| `GET /` | Static front end |
| `GET /api/matches` | The three storylines |
| `POST /api/matches/{id}/start?speed=` | Start a replay for this tab and return its `replay_id`. If 3 replays are already running, return 503 with `Retry-After` |
| `POST /api/replays/{replay_id}/control` | `{ "action": "pause" \| "resume" }`. Returns the replay's state. Pausing a paused replay or resuming a running one is a no-op |
| `GET /api/replays/{replay_id}/stream` | SSE: events, commentary lines (with `audio_id`), insights, recap. A heartbeat comment every 15 s. Disconnecting stops the replay |
| `POST /api/ask` | `{ replay_id, question }` → `Explanation`. The cut-off is the viewer's released event position on the server, never the agents' newer `as_of`; the client sends no minute |
| `POST /api/ask-audio` | Multipart `replay_id` + WAV audio → `{ transcript, explanation, audio_id }`. Server-side STT, then the same viewer-released-position cut-off as `/api/ask`, then TTS of the answer |
| `GET /api/lines/{line_id}/evidence` | The events and facts behind a commentary line |
| `GET /api/audio/{audio_id}` | A short MP3: a commentary line, a spoken answer or the audio recap. Held in memory or a temp directory and deleted when its replay stops |
| `GET /healthz` | Health probe |

The limits, all configurable, are:
- Question length: 300 characters maximum. This applies to the STT transcript too.
- Question audio: 15 seconds and 1 MB maximum.
- Viewer questions (typed and spoken combined): 6 per minute per client. A client is identified by its IP address from `X-Forwarded-For`, because Container Apps ingress is a proxy.
- Concurrent replays: 3.
- Tool calls per agent turn: 4 maximum.
- Time budgets: as in the table above.

Container Apps notes:
- State is in memory, so `maxReplicas` stays at 1.
- Check the current Container Apps ingress idle timeout on Learn and keep the SSE heartbeat well inside it.
- The README tells judges that the first load after idle can take a few seconds, because the app scales to zero.

## Observability and evaluation

- **Don't create GenAI spans by hand.** Agent Framework already emits `invoke_agent <name>`, `chat <model>` and `execute_tool <name>`, with `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` on the chat spans. Configure Azure Monitor once at startup (`configure_azure_monitor` from `azure-monitor-opentelemetry`, or `client.configure_azure_monitor()`; verify the current method on Learn). Keep `ENABLE_SENSITIVE_DATA=false` in Azure.
- Wrap each pipeline run in a parent span `pundit.turn` carrying `app.estimated_cost_usd`, `app.evidence.status`, `app.match_id` and `app.route` (`live`, `question` or `recap`). The evidence status is only known after the agent spans close, which is why it goes on the parent.
- `agents/pricing.py` holds dated list prices for the deployment or deployments in use. The UI shows tokens and the estimated cost on each answer.
- `observability/queries.kql` contains queries for cost per match, evidence failures and latency per agent.
- `evaluation/cases.yaml` holds 10–15 questions per storyline. Each case has the expected tools, the expected `event_ids` to cite and out-of-scope questions. `python -m evaluation` runs them and reports tool selection, evidence pass rate and latency.

## Phases and gates

| Phase | Plan day | Build | Gate evidence |
| :---- | :---- | :---- | :---- |
| 0 | Fri 16 | Repo skeleton, README stub, `.gitignore`, `pyproject` with pinned versions, CI that runs pytest (live tests skipped), azd template, `.env.example`. Verify the Agent Framework, Speech and Foundry docs and record the findings in `docs/DECISIONS.md`. That includes whether multi-round tool calls with `store: False` on our deployment need `default_options={"include": ["reasoning.encrypted_content"]}`, and the Python Speech SDK's Entra auth pattern | CI green, one successful `FoundryChatClient` call printed locally, and one tool-calling round trip with `store: False` |
| 1 | Sat 17 | Schema, squads, generator, three storylines, replay, all analytics tools (with `as_of` cut-off) and their tests. A console Commentator (single agent) | `pytest` output, and a console transcript of `m01` at 60x |
| 2 | Sun 18 | All five agents and the Workflow orchestration, the evidence check with tests (including deliberate failures and a future-event rejection), narrative modes, the cached outputs, and server-side TTS of the commentary with the Python Speech SDK | A transcript with event IDs, a test showing a fabricated number rejected, and an MP3 of 2 minutes of commentary |
| 3 | Mon 19 – Wed 21 | FastAPI, SSE, front end with pause and resume through `/control`, `/api/ask`, push-to-talk with `/api/ask-audio`, `/api/audio`, the evidence view, recap and audio recap | A screen recording of `m02` with one spoken question and the recap |
| 4 | Thu 22 – Fri 23 | Dockerfile, Bicep, `azd up`, managed identity roles, rate limits and the replay cap, tracing, pricing, the evaluation set and KQL | A public URL, the trace screenshot, and evaluation results |
| 5 | Sat 24 – Sun 25 | Polish, edge cases and on-error behaviour, the README (pitch, tech used, run locally, judge testing steps, the kill switch, attribution), and `docs/ARCHITECTURE.md` with a mermaid diagram | Fresh clone, then `azd up` works from the README alone |

## Definition of done (submission)

- [ ] Public GitHub repo, MIT licence, no secrets, no generated match data committed (cached outputs in `data/cached/` excepted)
- [ ] Live URL that judges can use until 10 Nov with no login, and `cached` mode works if the model is unavailable
- [ ] README: what it does, the problem it solves, the Microsoft tech used (Foundry, Agent Framework, Azure Speech, Container Apps, App Insights, GitHub Copilot), how to run it, how to test it, and attribution
- [ ] Demo video under 2 minutes on YouTube: fictional clubs only, no copyrighted music
- [ ] Evaluation results and a trace screenshot in `docs/`

## Out of scope

Real clubs, players or data; video or footage; user accounts; a database; AKS; more than one container; mobile apps; multiple languages; betting or odds of any kind; deployment from CI; resuming a replay after disconnect.
