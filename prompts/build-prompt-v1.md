# Build Prompt v1: Second Screen Pundit

*Generation prompt for a coding agent (GitHub Copilot agent mode in VS Code). Modelled on Brian Benz's `prompts/demopromptv1.md` in [bbenz/football-results-dashboard](https://github.com/bbenz/football-results-dashboard) (MIT). Copy this file into the new repo as `prompts/build-prompt-v1.md` and refer to it with `#file:prompts/build-prompt-v1.md`. Run **one phase per chat session**, so each session stays small and the agent stops at every gate.*

Plan and dates: [[Hackathon - Second Screen Pundit]]

---

## Non-negotiables

Read these first. Everything else elaborates on them.

1. **Build only what this prompt describes.** It is the requirements document. Don't add features, pages or services it doesn't mention. If something seems missing, ask.
2. **Ship running code, not plans.** Never claim a command, deployment, model call or test passed without showing the output that proves it.
3. **Check before you design around something.** Verify Microsoft Agent Framework, Microsoft Foundry, Azure Speech, Azure Container Apps, azd and Bicep behaviour against current Microsoft Learn documentation before relying on it. Don't assume a preview feature, SKU, package name or API version exists. If the docs and this prompt disagree, stop and tell me.
4. **Deterministic code owns every number and every fact.** Analytics tools compute all statistics from the match event log. Models choose tools, decide what matters and explain results. They never compute, invent or override a statistic. Every number and every event a model output mentions must trace back to a tool result, and an automated check must prove it.
5. **All match data is synthetic.** Use fictional clubs, players and stadiums only. No real club, player, league, badge, crest or trademark appears anywhere: not in code, data, the UI, tests, the README or the video. No match footage.
6. **Use keyless access everywhere.** Use managed identity in Azure and `DefaultAzureCredential` locally. Never write, log, print or commit keys, connection strings or tokens. The browser gets only short-lived Speech tokens issued by the API.
7. **Cost guardrails are features, not extras.** Every model call has `max_output_tokens`, a time budget and `store: false`. The public API is rate-limited per client. Container Apps scale to zero.
8. **Keep it one container.** A single Python API serves the static front end, the event stream and the agents. Don't split it into microservices.
9. **Stop at each phase gate.** Report what was built, the evidence that it works (command output, test results) and anything you are unsure about. Then wait for me. Don't start the next phase on your own.
10. **Make routine engineering choices yourself.** Don't ask permission to write code, add a test or pick a reasonable library version.

## Mission

Build **Second Screen Pundit** for the Microsoft x Premier League *Inside the Game* developer hackathon (submission deadline 27 Oct 2026). The brief is: AI-powered solutions that turn synthetic, football-realistic data into explainable match intelligence, real-time narratives and automated match recaps for studio, broadcast or streaming use.

The app replays a synthetic football match as a live event stream. A team of agents watches it, decides what's worth saying, and produces short spoken commentary. Viewers can ask spoken or typed "why" questions and get answers that cite the exact events behind them. At full time the app produces a written recap and an audio recap. **Every claim links to event IDs, so the commentary is explainable rather than just plausible.**

Judging gives equal weight to technological implementation, agentic design and innovation, real-world impact, UX and presentation, and fit with the hackathon category. Target prizes: Best Multi-Agent, then Best Use of Microsoft Foundry.

## Stack

- Python 3.12, FastAPI, uvicorn, pydantic v2, pytest.
- **Microsoft Agent Framework** (Python) with `FoundryChatClient` against a Microsoft Foundry project. Verify the current package name and install command on Learn.
- **Two model settings**, set by configuration and never hard-coded. They may point at the same deployment. *Current setup: both use `gpt-5.4-mini` (Data Zone Standard, Foundry resource in Australia East), because Sol quota was refused.*
  - `MODEL_FAST`: a mini model for the Director and Commentator (GPT-5.4 mini if available in the region, otherwise `gpt-6-sol`).
  - `MODEL_DEEP`: `gpt-6-sol` for the Tactics Explainer and Recap Producer.
- **Azure Speech**: neural TTS for commentary and recap, and STT for push-to-talk questions, through the Speech SDK for JavaScript in the browser using a token from `GET /api/speech-token`.
- Front end: one HTML page with vanilla JS and CSS, no build step, served by FastAPI. It has a live timeline, a pitch mini-map, a commentary transcript, a "why?" button on each line, an ask box with a mic button, and a recap tab.
- OpenTelemetry to Azure Monitor / App Insights, using `gen_ai.*` semantic conventions.
- Infrastructure: `azd` with Bicep. Azure Container Apps (consumption, `minReplicas: 0`), Azure Container Registry, user-assigned managed identity, Foundry account with project and both deployments (`versionUpgradeOption: NoAutoUpgrade`), Speech, Log Analytics, App Insights, and a **budget resource (£75 per month, with alerts at 50%, 80% and 100%)**.
- **No local Docker.** Build images remotely with Azure Container Registry (`docker: remoteBuild: true` in `azure.yaml`). Don't require Docker Desktop on the developer machine.
- GitHub Actions: run tests on every push, deploy with `azd` on push to `main`.

## Synthetic match data

### Event schema (`engine/schema.py`)

Each event is a pydantic model:

| Field | Notes |
| :---- | :---- |
| `event_id` | Stable string, `{match_id}-e{seq:04d}`, e.g. `m01-e0042` |
| `match_id`, `seq`, `minute`, `second`, `period` | `period` is 1 or 2 |
| `team` | `home` or `away` |
| `player_id` | References the match's fictional squad |
| `type` | `kickoff`, `pass`, `carry`, `shot`, `goal`, `save`, `tackle`, `interception`, `foul`, `card`, `substitution`, `formation_change`, `half_time`, `full_time` |
| `x`, `y` | Pitch coordinates 0–100, attacking left to right for the home team |
| `outcome` | `success` or `fail`, where relevant |
| `xg` | Shots only, 0–1 |
| `detail` | Type-specific dict, e.g. card colour, sub in/out, new formation |
| `related_event_ids` | E.g. the pass that led to a shot |

### Generator (`engine/generate.py`)

- It is seeded and deterministic: the same seed gives the same match. It produces a realistic event rate (about 1,500–2,000 events per match), possession swings, xG that loosely matches the score, and formation changes after red cards and substitutions.
- It ships three **storyline scripts**, each a seed plus scripted beats:
  - `m01` **Late comeback**: 0–2 at 70', 3–2 at full time after a switch to a back three.
  - `m02` **Red card reshape**: a 38' red card forces a 4-4-1 and the away team camps in the final third.
  - `m03` **Pressing masterclass**: the home team wins on high turnovers. Low xG for the away team.
- It uses fictional clubs and squads (`engine/squads.yaml`), for example "Ashbridge Rovers" and "Port Calder FC". Names must not resemble real clubs.
- It writes to `data/matches/{match_id}.jsonl` and generates at startup if the file is missing. Generated files are build artefacts, so add them to `.gitignore`.

### Replay (`engine/replay.py`)

- It streams events at a configurable speed (1x to 120x) with pause and seek.
- It publishes to an in-process async queue that feeds the agents and the server-sent events (SSE) stream.

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

Every tool gets unit tests on hand-built mini fixtures with hand-calculated expected values.

## Agents (`agents/`)

Build them with Microsoft Agent Framework. Every agent returns structured JSON validated by pydantic, and every output carries `fact_ids` and `event_ids`.

1. **Director** (`MODEL_FAST`), the orchestrator.
   - **Deterministic pre-filter:** goals, cards, substitutions, formation changes and big chances (xG > 0.25) always go forward. Otherwise it sends one event window every 3 match-minutes.
   - **Model decision:** it decides whether the window is worth commentary (an `importance` score from 0 to 1, with a threshold from config) and which agent handles it.
   - **Cooldown:** at most one commentary line per 20 seconds of real time.
   - **Viewer questions** go to the Tactics Explainer. **Full time** triggers the Recap Producer.
   - Use the Agent Framework group-chat or handoff orchestration, whichever the current docs recommend for an agent-based orchestrator. Document the choice in `docs/ARCHITECTURE.md`.
2. **Match Analyst** (`MODEL_FAST`). It calls the tools and returns an `Insight { insight_id, minute, headline, fact_ids, event_ids, importance }`.
3. **Commentator** (`MODEL_FAST`). It turns an insight into a `CommentaryLine { text (≤ 200 chars, 1–2 sentences), insight_id, fact_ids, event_ids, tone }`. It has a broadcast persona, British English, no hype words, and is never allowed to state a number that isn't in the insight's facts.
4. **Tactics Explainer** (`MODEL_DEEP`). It answers a viewer's question with `Explanation { answer (≤ 80 words), claims: [{ text, fact_ids, event_ids }], cannot_tell }`. It calls tools as needed. If the data can't answer the question, it says so in `cannot_tell` and doesn't guess.
5. **Recap Producer** (`MODEL_DEEP`). At full time it produces `Recap { headline, summary (≤ 200 words), key_moments: [3 × { minute, text, event_ids }], audio_script (≤ 150 words), fact_ids }`.

### Evidence check (`agents/evidence.py`, plain code, no LLM)

Run it on every agent output before it reaches the viewer:

1. **Event check:** every `event_id` cited must exist in the match log at or before the current replay minute. Commentary can't reference the future.
2. **Fact check:** every `fact_id` must exist in this turn's tool results.
3. **Number check:** every number in the text must match a tool result value at the precision written. Adapt `agent/grounding.py` from bbenz/football-results-dashboard (MIT) and credit it in the file header and README.
4. **If a check fails:** retry once with the failure as feedback. If it fails again, drop the commentary line (live), or return the deterministic facts with a visible notice (Q&A and recap).

The **narrative mode** setting is `live | cached | off`. In `cached` mode the app replays labelled pre-generated outputs for the three storyline matches, as a fallback during judging.

## API (`api/`)

| Endpoint | Purpose |
| :---- | :---- |
| `GET /` | Static front end |
| `GET /api/matches` | The three storylines |
| `POST /api/matches/{id}/start?speed=` | Start or restart a replay (one replay per session) |
| `GET /api/matches/{id}/stream` | SSE: events, commentary lines, insights, recap |
| `POST /api/ask` | `{ match_id, minute, question }` → `Explanation` |
| `GET /api/lines/{line_id}/evidence` | The events and facts behind a commentary line |
| `GET /api/speech-token` | Short-lived Speech token plus region (managed identity) |
| `GET /healthz` | Health probe |

The limits, all configurable, are:
- Question length: 300 characters maximum.
- Viewer questions: 6 per minute per client.
- Tool calls per agent turn: 4 maximum.
- Time budget: 20 seconds per answer.

## Observability and evaluation

- Trace spans: `invoke_agent {name}`, `chat {deployment}` and `execute_tool {name}`. Each records `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `app.estimated_cost_usd`, `app.evidence.status` and `app.match_id`.
- `agents/pricing.py` holds dated list prices for both deployments. The UI shows tokens and the estimated cost on each answer.
- `observability/queries.kql` contains queries for cost per match, evidence failures and latency per agent.
- `evaluation/cases.yaml` holds 10–15 questions per storyline, each with the expected tools, the expected `event_ids` to cite and out-of-scope questions. `python -m evaluation` runs them and reports tool selection, evidence pass rate and latency.

## Phases and gates

| Phase | Plan day | Build | Gate evidence |
| :---- | :---- | :---- | :---- |
| 0 | Fri 16 | Repo skeleton, README stub, `.gitignore`, `pyproject`, CI that runs pytest, azd template, `.env.example`. Verify the Agent Framework, Speech and Foundry docs and record the findings in `docs/DECISIONS.md` | CI green, one successful `FoundryChatClient` call printed locally |
| 1 | Sat 17 | Schema, squads, generator, three storylines, replay, all analytics tools and their tests. A console Commentator (single agent) | `pytest` output, and a console transcript of `m01` at 60x |
| 2 | Sun 18 | All five agents and the orchestration, the evidence check with tests (including deliberate failures), narrative modes, and local TTS of the commentary | A transcript with event IDs, a test showing a fabricated number rejected, and an audio file of 2 minutes of commentary |
| 3 | Mon 19 – Wed 21 | FastAPI, SSE, front end, `/api/ask` with the mic, the evidence view, recap and audio recap | A screen recording of `m02` with one spoken question and the recap |
| 4 | Thu 22 – Fri 23 | Dockerfile, Bicep, `azd up`, managed identity roles, rate limits, tracing, pricing, the evaluation set and KQL | A public URL, the trace screenshot, and evaluation results |
| 5 | Sat 24 – Sun 25 | Polish, edge cases and on-error behaviour, the README (pitch, tech used, run locally, judge testing steps, attribution), and `docs/ARCHITECTURE.md` with a mermaid diagram | Fresh clone, then `azd up` works from the README alone |

## Definition of done (submission)

- [ ] Public GitHub repo, MIT licence, no secrets, no generated data committed
- [ ] Live URL that judges can use until 10 Nov with no login, and `cached` mode works if the model is unavailable
- [ ] README: what it does, the problem it solves, the Microsoft tech used (Foundry, Agent Framework, Azure Speech, Container Apps, App Insights, GitHub Copilot), how to run it, how to test it, and attribution
- [ ] Demo video under 2 minutes on YouTube: fictional clubs only, no copyrighted music
- [ ] Evaluation results and a trace screenshot in `docs/`

## Out of scope

Real clubs, players or data; video or footage; user accounts; a database; AKS; more than one container; mobile apps; multiple languages; betting or odds of any kind.
