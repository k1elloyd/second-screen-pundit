# Phase 0 decisions

Verified on 2026-10-16 against Microsoft Learn and the current package index.

## Microsoft Agent Framework and Foundry

- Use `agent-framework-foundry`; the current Foundry provider documentation installs this package and imports `FoundryChatClient` from `agent_framework.foundry`.
- Configure `FoundryChatClient` with `project_endpoint`, the model **deployment name**, and an Azure Identity `TokenCredential`. The project endpoint shape is `https://<account>.services.ai.azure.com/api/projects/<project>`.
- Use `Agent` with local Python callables in `tools`. The client connects to the Foundry Responses endpoint and supports function tools.
- Keep `store: False` and `max_tokens` in the agent's `default_options`. Microsoft Learn says `FoundryChatClient` does not request encrypted reasoning by default. For reasoning-capable deployments using stateless multi-turn Responses, include `reasoning.encrypted_content` so reasoning context can continue across turns.
- The current package index reported `agent-framework-foundry==1.15.0`. The current Foundry provider documentation recommends the package above and documents the endpoint, credentials, tools, and encrypted-reasoning option.

## Azure Speech authentication

- The Python Speech SDK package is `azure-cognitiveservices-speech` (imported as `azure.cognitiveservices.speech`).
- Microsoft Learn's Python Entra pattern creates `SpeechConfig(token_credential=<Azure Identity credential>, endpoint="https://<custom-subdomain>.cognitiveservices.azure.com/")`. Apply the same pattern to recognizers and synthesizers; use `DefaultAzureCredential` rather than a subscription key or browser-visible token.
- Speech Entra authentication requires a custom domain and either the *Cognitive Services Speech User* or *Cognitive Services Speech Contributor* role. The custom domain cannot be changed after it is created.
- Pass the credential object rather than storing an access-token string. Learn documents the token-credential constructor but does not prescribe a refresh interval for Python; validate renewal on the long-lived Speech path when that phase is implemented.
- The current package index reported `azure-cognitiveservices-speech==1.52.0`.

## Project and test packages

The following exact releases were reported by the package index on 2026-10-16 and are pinned in `pyproject.toml`:

| Package | Version |
| :--- | :--- |
| `agent-framework-foundry` | `1.15.0` |
| `azure-cognitiveservices-speech` | `1.52.0` |
| `azure-identity` | `1.26.0` |
| `fastapi` | `0.143.2` |
| `pydantic` | `2.14.1` |
| `uvicorn` | `0.54.0` |
| `pytest` | `9.1.1` |
| `python-dotenv` | `1.2.4` |
| `setuptools` | `84.0.0` |

## azd template

- Microsoft Learn's azd schema permits `language: python` and `host: containerapp`.
- For container apps, the schema supports a `docker` block. The azd remote-build guide documents `path: ./Dockerfile` and `remoteBuild: true`; it submits the build to Azure Container Registry. The template scaffold uses that setting and does not require local Docker for a successful remote build. Learn notes that azd falls back to Docker or Podman locally if the remote build fails, so that fallback will not work on a machine without a local container engine.
- Phase 0's Bicep file is intentionally a no-resource placeholder; actual Azure resources and deployment are out of scope until their specified phase. `az bicep build --file infra/main.bicep --stdout` succeeded with Bicep `0.48.1.52935`. The compiler reported only `no-unused-params` warnings for the two Phase 4 parameters, which are mapped in `infra/main.parameters.json`.

## Live gate status

- The root `.env` was made available for the live gate. Both marked Foundry tests passed using `DefaultAzureCredential`: a chat response printed `PONG`, and the tool-call round trip printed `COPPER`. The tool-call test used `store: False`, `max_tokens: 96`, a 30-second timeout, and `include: ["reasoning.encrypted_content"]`.
- A separate bounded tool-call round trip also returned `COPPER` with `store: False` and no `reasoning.encrypted_content` include. Therefore, this one-function tool cycle did not require the include to complete on the configured deployment. It does not prove that longer reasoning/tool cycles can omit it; retain the include when preserving reasoning state is needed, as described by the Responses API documentation.
- No endpoint, deployment, token, or credential value was printed or copied into the repository.

## Phase 1 implementation

- The installed `agent-framework-foundry==1.15.0` exposes `Agent`, `FoundryChatClient`, asynchronous `Agent.run`, Pydantic structured responses, and `response.value`. The current Microsoft Learn provider page confirms `FoundryChatClient` uses a project endpoint and deployment name with Azure Identity credentials. The structured-output page confirms `response_format` can be set through `Agent.default_options` and parsed output is available from `response.value`.
- The console Commentator has no tools. It uses `DefaultAzureCredential`, structured `CommentaryLine` output, `max_tokens: 256`, `store: False`, and a 15-second request timeout. Commentary text is plain spoken English; event and fact citations remain in their structured ID fields and a deterministic post-check rejects inline citation markers.
- YAML configuration is package data, parsed with `yaml.safe_load`. `PyYAML==6.0.3` is pinned directly because the engine reads the curated squads, storyline scripts, and analytics thresholds.
- The curated club-name test stores SHA-256 fingerprints rather than literal names from its small denylist, so the test itself does not introduce real club names into source or test data.

## Phase 2 implementation

- A single `create_foundry_agent` factory builds all five roles with `max_tokens`, `store: False`, the Pydantic `response_format`, `include: ["reasoning.encrypted_content"]`, and a four-function-call limit. The Foundry Responses request uses `reasoning={"effort": "low"}` for the Director and Commentator; it does not use the Chat Completions `reasoning_effort` option.
- `WorkflowBuilder`/`AgentExecutor` is used for the standalone Explainer and Recap runs. The live route is a typed `WorkflowBuilder` graph that conditionally routes a must-say window to its fast-path executor or a periodic window to its tactical-path executor. Function middleware captures serialized analytics-tool results; evidence checks use those captured facts and the run's fixed `as_of` event sequence.
- The Speech synthesizer uses `SpeechConfig(token_credential=..., endpoint=...)`, the SDK's MP3 output-format enum, and the `SpeechSynthesisResult.audio_duration` property to verify actual output duration. `SPEECH_ENDPOINT` must be a custom Speech endpoint; local credentials are provided as a `TokenCredential` from `DefaultAzureCredential`. `SPEECH_REGION` remains required in configuration, but the Python SDK treats endpoint and region as alternative connection modes and rejects passing both; custom-endpoint Entra authentication uses the endpoint form.
- Microsoft Learn's Python Speech reference describes `audio_duration` as the time duration of synthesized audio and marks it available since Speech SDK 1.21.0. The project pins `azure-cognitiveservices-speech==1.52.0`, so the property is available.

## Phase 2b: live latency

Measured on 2026-10-16 with the m01 goal event `m01-e0395` during a 20x replay. The 15-second live-window budget was unchanged.

| Measurement | Result |
| :--- | :--- |
| Before: existing must-say route | Timed out at 15,553 ms; Analyst 13,603 ms, Commentator 1,807 ms |
| After: deterministic must-say fast path | Passed in 7,472 ms; Commentator 7,326 ms |
| Budget | 15,000 ms |

The pre-change route did not finish before the deadline. The gate's per-agent timings are wall-clock durations and identify the retry attempt. Must-say events (goals, cards, substitutions, formation changes, and big chances) now use the deterministic Phase 1 console insight and make one Commentator call; they do not call the Director or Analyst. Periodic tactical windows retain Director → Analyst → Commentator, and their result is dropped when the caller marks its window stale. The score phrase is calculated from deterministic match-state facts and supplied to the Commentator, which is instructed to reuse it verbatim rather than orient the score. The console replay default is 20x.

### Warm-up and full-match measurements

Before the warmed full-match run, the gate explicitly acquired a token from the existing `DefaultAzureCredential` for the documented `https://ai.azure.com/.default` scope and made one 16-token `PONG` request. The warm-up took 4,098 ms for token acquisition, 7,792 ms for the model request, and 12,207 ms total. The same credential object was used by the replay.

The warmed m01 replay completed at 20x in 356,478 ms. Each live window retained the 15,000 ms budget.

| Path | Windows | Lines | Commentator time per emitted line | Dropped lines |
| :--- | ---: | ---: | :--- | :--- |
| Must-say fast path | 13 | 13 | 13 samples; median 6,807 ms; slowest 13,118 ms | 0 |
| Tactical | 30 | 0 | No Commentator calls; median and slowest not applicable | 30 |

The 30 tactical windows had 27 Director decisions not to comment and 3 windows dropped as stale before commentary completed. The full-match gate prints each agent call's role, attempt, and wall-clock duration alongside these summaries.

### Tactical candidates, broadcast delay and token breakdown

These measurements supersede the table above. Replay: m01 at 20x, 15,000 ms budget unchanged, `python -m second_screen_pundit.agents.gate --live-latency`. The gate logs every call's duration, input, output and reasoning tokens.

**What the measurements changed**

- **A new client costs seconds.** The pipeline built a new `FoundryChatClient` for every call, and the earlier warm-up warmed a different client from the one the live calls used. Isolated probe, same Commentator prompt: median 4,376 ms with a fresh client per call, 1,777 ms with one reused client; after a tiny unstructured call on the shared client, the first structured call took 1,762 ms. The pipeline now keeps one client per deployment and budget, and `PunditPipeline.warm_up()` fetches the `https://ai.azure.com/.default` token and makes one 16-token call on that client. Warm-up in the three runs: 9.4-12.2 s (token 2.6-3.8 s, model call 6.6-8.2 s), logged separately from per-window timings.
- **`max_tokens` counts reasoning tokens.** A Commentator reply at `low` effort is about 50-200 reasoning tokens plus about 100 JSON tokens; the 256 cap truncated some replies, and `response.value` then raised a raw `ValidationError` that escaped the retry logic. Caps are now Director 320, Commentator 400 (the m01 substitution line used 297 output tokens, 197 of them reasoning, so it would have exceeded 256), Analyst 384. Truncated or off-schema output is now retried like any other failure.
- **The retry replayed the whole chain.** One bad reply re-ran Director, Analyst and Commentator, which cannot fit in 15 s. In the first rerun's log the expired windows I traced (21' and 81') had each started a second Director and Analyst pass before the deadline. A window still gets one retry inside the same 15 s deadline, but it resumes at the step that failed and keeps the validated Director decision and Insight. A window that fails twice with malformed output is now reported as such, not as an evidence failure.
- **Raw events were the Director and Analyst input.** The old prompts carried every event in a 3-minute window. They now carry the deterministic signals, and the Commentator receives only the facts the Insight cites.

**Tactical lane** (`agents/signals.py`, `agents/dispatcher.py`; thresholds in `engine/analytics.yaml`)

Every 3 match minutes the dispatcher evaluates deterministic signals at that boundary's `as_of`. Each signal's numbers come from the existing metric functions and are exposed as `tactical_signals:*` facts with unique IDs (the existing tools reuse IDs such as `compare_windows:before`, which collide when two calls occur in one run) and at most 5 evidence event IDs.

| Signal | Rule | Mandatory |
| :--- | :--- | :---: |
| `possession_swing` | A team's possession share in the last 10 minutes exceeds the previous 10 by more than 15 percentage points | no |
| `xg_surge` | A team's xG in the last 10 minutes exceeds 0.4 **and** exceeds the opponent's in the same window | no |
| `substitution_effect` | Once per substitution, 5 minutes after it: pressing changed by at least 0.5 defensive actions/min or high turnovers by at least 3, compared with the 5 minutes before | no |
| `red_card` | A red card is seen (facts: players left on the pitch) | yes |
| `formation_change` | A formation change is seen (facts: new and previous formation) | yes |

- **Literal 0.4 xG fires too often here.** On m01 and m02 a team exceeds 0.4 xG in 10 minutes at 46-48% of boundaries (median per team 0.35), so the opponent condition is my addition to keep "surge" meaning one team on top. The 15-point swing fires at 17-29% of boundaries (median absolute swing about 9 points).
- **Mandatory signals skip the Director model.** The code forwards them deterministically, because the requirement is that these beats produce a tactical line and a model call that cannot change the outcome only spends budget. They still run Analyst and Commentator. Other signals go to the Director with a threshold of 0.45.
- **Cooldowns, in match minutes.** Any tactical dispatch needs `COMMENTARY_COOLDOWN_MINUTES` (4) since the last tactical dispatch. Non-mandatory signals also need `TACTICAL_COOLDOWN_MINUTES` (10, new) since the last tactical *line*, and no tactical run in flight. Pending one-shot signals wait up to 10 minutes. Must-say events are exempt, as the prompt specifies.
- **Stale.** A tactical run is dropped if a newer tactical window has been dispatched before it finishes. Must-say events run in their own lane and do not make a tactical run stale. This is my reading of the prompt's backpressure rule ("a must-say event replaces a *pending* ordinary window"); under the earlier harness, any must-say event staled an in-flight tactical run, which is why 3 of the 30 windows were dropped as stale.
- **Dry run, no model calls, over the three matches:** m01 dispatches 7 windows including the 69' formation change (4-2-3-1) and the 75' back-three switch; m02 dispatches 7 including the 39' red card and the 45' formation change (4-4-1); m03 dispatches 5. m03's pressing storyline has no red card or substitution, so none of the specified signals covers it.

**Broadcast delay** (`engine/broadcast.py`, `BROADCAST_DELAY_SECONDS`, default 10)

Agents read events as they are published and stamp each event's arrival. The viewer-facing stream releases each event once it is `BROADCAST_DELAY_SECONDS` old, and a line waits for the release time of its own `as_of` event. Agents' `as_of` is unchanged. A line that is ready after its event's release time is released immediately and its lateness is recorded; it is not dropped, because the budget stays 15 s. Phase 3 must answer viewer questions with the viewer's released `seq`, not the agents' newer one, or an answer can reveal an event the viewer has not seen. `asyncio.sleep` can wake early on Windows, so the wait loops until the release time has actually passed.

**Results, m01 at 20x, Commentator effort `low` (the default)**

| Path | Windows | Lines | Commentator per line (median / slowest) | Whole run (median / slowest) | Dropped |
| :--- | ---: | ---: | :--- | :--- | :--- |
| Must-say fast path | 13 | 13 | 2,785 / 3,933 ms | 2,796 / 3,937 ms | 0 |
| Tactical | 30 boundaries: 7 no signal, 15 held by cooldown, 8 dispatched | 6 | 2,274 / 2,714 ms | 7,977 / 10,250 ms | 2 (Director declined) |

Before (previous section): fast path median 6,807 ms, slowest 13,118 ms; tactical 0 lines from 30 windows.

Tactical lines produced: 21' possession swing, 33' xG surge, 57' xG surge, 69' formation change to 4-2-3-1, **75' formation change to 3-4-3** ("Ashbridge Rovers have gone more aggressive, moving from a 4-2-3-1 to a 3-4-3. Port Calder lead 2-1."), 87' xG surge. That is one line per 15 minutes. The Director declined the windows at 45' and 51'; a decline does not start the 10-minute cooldown, which is why a second window was dispatched six minutes after the first (the dry run, which has no declines, dispatches 7 windows, not 8). Evidence checks passed on every line shown.

| Role | Calls | Median duration | Median input / output / reasoning tokens |
| :--- | ---: | ---: | :--- |
| Director | 6 | 2,980 ms | 551 / 147 / 101 |
| Analyst | 6 | 2,888 ms (slowest 5,242 ms) | 1,099 / 111 / 0 |
| Commentator | 19 | 2,606 ms | 636 / 196 / 93 |

The Analyst's slow calls are the ones that call a tool: `match_state` returns every successful pass as evidence, so the follow-up request carries about 7,300 input tokens instead of about 1,100.

Viewer stream: 1,548 events released, observed delay 10.000-10.032 s. Of 19 lines, 18 were ready before their event's release and 1 was ready 250 ms after it (the 87' tactical line, whose Analyst call took 5.2 s).

**Reasoning effort below `low`.** Microsoft Learn's reasoning-model page lists `none` for `gpt-5.4` but does not list `gpt-5.4-mini` in that footnote, and says `minimal` works only with the original GPT-5 models, so Learn does not confirm a level below `low` for this model. The deployment accepted `none` (0 reasoning tokens), so I measured it for the Commentator only (`COMMENTATOR_REASONING_EFFORT`, default `low`):

| Commentator effort | Commentator call median | Output tokens median | Fast path median / slowest | Tactical lines | Tactical windows needing a Commentator retry |
| :--- | ---: | ---: | :--- | ---: | ---: |
| `low` | 2,606 ms (19 calls) | 196 (93 reasoning) | 2,785 / 3,933 ms | 6 | 0 |
| `none` | 2,061 ms (22 calls) | 105 (0 reasoning) | 2,643 / 3,249 ms | 5 | 3 |

`none` saves about 0.5 s per call (21%), but in this sample 3 tactical windows needed a Commentator retry (two recovered; the 87' window was lost to an ungrounded number word, "ten"), and `low` needed none. Isolated probe, 4 calls each on one reused client: 1,777 ms median at `low`, 1,463 ms at `none`. Each configuration is a single replay, so the retry difference is suggestive, not established. I did not adopt `none`: it is undocumented for this model and the fast path already has about 11 s of headroom.

### Phase 2b follow-up: goal consolidation, evidence size and pressing signal

**Design changes**

- A shot linked to a later goal is held out of the fast path. The goal produces the single line, with deterministic facts for both the score and the linked shot's xG, so both event IDs remain available to the evidence check.
- Analytics tools retain all computed summary facts but return no more than five distinct evidence event IDs per tool result. The evidence check and Analyst pipeline use the captured, compact results; an Insight cannot cite an event ID absent from those results.
- `high_turnover_surge` compares each team's successful tackles/interceptions at `x >= 60` in the last 10 match minutes with its match-to-date average per 10 elapsed match minutes, using only events through the boundary's `as_of`. It fires at or above twice that average and is a non-mandatory Director candidate.
- The Phase 3 requirements now explicitly bind typed and spoken viewer Q&A to the viewer's released event position, not the agents' newer `as_of`.

**Dry run: dispatched tactical windows, no model calls**

| Match | Windows and signals |
| :--- | :--- |
| m01 | 21' possession swing; 33' xG surge; 45' xG surge; 57' xG surge + high-turnover surge; 69' formation change; 75' formation change; 87' xG surge + high-turnover surge |
| m02 | 18' xG surge; 30' possession swing + xG surge; 39' red card; 45' formation change; 57' xG surge; 69' xG surge; 81' possession swing + xG surge + high-turnover surge |
| m03 | 21' high-turnover surge; 39' possession swing; 54' xG surge; 66' high-turnover surge; 81' xG surge |

The m01 back-three switch and m02 red-card reshape remain mandatory signal windows. The new m03 signal identifies the scripted pressing storyline at 21' and 66'.

**Analyst input-token comparison**

The previous full-match run had a median of 1,099 Analyst input tokens; tool-expanded match-state calls reached about 7,300 tokens. In the latest run the median was 1,326 tokens (7 calls), with the largest logged Analyst input at 2,346 tokens. The median is not directly comparable because the run's signal mix and tool calls differ, but the largest tool-expanded prompt fell by about 68%. Each tool result now carries at most five distinct evidence event IDs, while all deterministic summary facts remain intact.

**Live gate: m01 at 20x**

Command: `.venv\Scripts\python.exe -m second_screen_pundit.agents.gate --live-latency --match m01 --speed 20`. The 15,000 ms budget is unchanged. Warm-up was 17,223 ms total (5,818 ms token acquisition, 10,983 ms model call), reported separately from replay timings.

| Path | Windows | Lines | Commentator time per emitted line | Whole run per emitted line | Dropped lines |
| :--- | ---: | ---: | :--- | :--- | :--- |
| Must-say fast path | 8 | 8 | 8 samples; median 2,591.5 ms; slowest 5,297 ms | median 2,602 ms; slowest 5,297 ms | 0 |
| Tactical | 8 dispatched (30 boundaries) | 6 | 6 samples; median 2,537.5 ms; slowest 5,036 ms | median 8,273.5 ms; slowest 10,656 ms | 2 (Director declined) |

Fast-path line count is lower than the previous 13 because each scoring shot and its linked goal now produce one line. Tactical lines produced at 21', 33', 57', 69', 75' and 87'; the 75' line covers Ashbridge Rovers' change to a 3-4-3. No run exceeded the unchanged 15-second budget. The two late viewer-facing lines followed event release by at most 656 ms; all 1,548 events were released after 10.000–10.047 seconds.

Role-level Analyst timing was median 2,989 ms (slowest 5,927 ms), with median input/output/reasoning tokens of 1,326/123/0. The full gate reported 15 Commentator calls, including retries, and all 14 produced lines passed evidence checks.

### Phase 2b review fixes

- Each analytics fact ID has a unique per-fact component; `check_evidence` rejects any duplicate fact ID across the results supplied for one run. Cached outputs use compatibility IDs only when their deterministic check contains a single corresponding tool result.
- Analytics and tactical facts identify the team (or explicitly say match-level), metric, and relevant minute/window in their labels. The score and match-total shot count are explicitly match-level.
- For each numeric mention, the evidence check requires an exact numeric magnitude in a cited fact; it applies no rounding tolerance. A team-specific fact can ground the number only when its team and metric are named in the same sentence. A match-level score is checked as a score expression or score-oriented phrase. This is not a general semantic-entailment check: it does not establish every nonnumeric claim, causal explanation, or whether a model paraphrase preserves the full meaning of a fact.
- After an agent or analytics-tool exception, including throttling and invalid tool arguments, Q&A and recap retry once and then return deterministic verified facts with a visible notice. Cancellation remains uncaught so stopping a run does not become a success-shaped fallback.
- The live `WorkflowBuilder` graph routes `must_say` windows through `live_path_router` to `must_say_fast_path`; other windows go to `tactical_path`. The role sequence and evidence checks run within the selected route executor. Replay stop cancels and awaits pending dispatcher tasks; the latency gate connects this to `Replay.state`.
- Speech synthesis explicitly uses `audio_config=None`, which prevents playback to the default speaker while retaining generated audio data.

### Post-review live gate: m01 at 20x

Command: `.venv\Scripts\python.exe -m second_screen_pundit.agents.gate --live-latency --match m01 --speed 20`. The 15,000 ms per-window budget was unchanged. The Foundry warm-up took 12,363 ms total (5,006 ms for token acquisition and 7,101 ms for the model call), separately from the replay. The replay completed in 294,528 ms.

| Path | Windows | Lines | Commentator time per emitted line | Whole run per emitted line | Dropped lines |
| :--- | ---: | ---: | :--- | :--- | :--- |
| Must-say fast path | 8 | 6 | 6 samples; median 3,062.5 ms; slowest 4,295 ms | median 3,172 ms; slowest 4,407 ms | 2 (evidence check failed twice for ungrounded `0.31` and `0.34`) |
| Tactical | 9 dispatched (30 boundaries) | 3 | 3 samples; median 4,504 ms; slowest 5,543 ms | median 10,640 ms; slowest 11,516 ms | 6 (budget expired: 1; Director declined: 1; invalid/truncated output: 2; unsupported formation: 1; unreturned evidence event IDs: 1) |

All nine emitted lines completed within the 15,000 ms budget; one tactical run reached the budget and was dropped. Tactical lines were produced at 21' (possession swing), 39' (xG surge), and 81' (possession swing). At the 30 boundaries, 7 had no signal, 9 were dispatched, and 14 were held by cooldown.

Role-level call timings (including retries): Director, 7 calls, median 2,765 ms and slowest 3,582 ms; Analyst, 11 calls, median 2,898 ms and slowest 5,613 ms; Commentator, 21 calls, median 2,708 ms and slowest 5,209 ms. Median Analyst input/output/reasoning tokens were 1,393/131/0; Commentator 765/274/123; Director 651/154/112. The viewer stream released all 1,548 events after 10.000–10.469 seconds; 2 lines were ready after their event release, by at most 1,828 ms. The full suite passed: 73 passed, 2 deselected.

### Phase 2 audio-duration gate clarification

`run_phase2_gate` synthesizes validated cached lines and repeats the first two lines to reach the 120-second duration threshold. This verifies synthesis and artifact duration; it is padded cached narration, not two minutes of unique or live commentary.

## Microsoft Learn sources

- [Azure OpenAI reasoning models: reasoning effort and API support](https://learn.microsoft.com/azure/foundry/openai/how-to/reasoning#reasoning-effort)
- [Authentication and authorization in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/concepts/authentication-authorization-foundry)
- [Microsoft Foundry model provider for Python](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
- [Producing structured outputs with agents](https://learn.microsoft.com/en-us/agent-framework/agents/structured-outputs)
- [Agent Framework workflow capabilities](https://learn.microsoft.com/en-us/agent-framework/workflows/)
- [Agent Framework workflow concepts and execution model](https://learn.microsoft.com/en-us/agent-framework/concepts/workflows/)
- [Use the Azure OpenAI Responses API: encrypted reasoning items](https://learn.microsoft.com/azure/foundry/openai/how-to/responses#encrypted-reasoning-items)
- [Microsoft Entra authentication with the Speech SDK](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-configure-azure-ad-auth?pivots=programming-language-python)
- [SpeechConfig Python API reference](https://learn.microsoft.com/python/api/azure-cognitiveservices-speech/azure.cognitiveservices.speech.speechconfig?view=azure-python)
- [SpeechSynthesisResult Python API reference](https://learn.microsoft.com/en-us/python/api/azure-cognitiveservices-speech/azure.cognitiveservices.speech.speechsynthesisresult?view=azure-python)
- [Azure Developer CLI schema reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Remote builds with Azure Container Registry](https://learn.microsoft.com/azure/developer/azure-developer-cli/remote-builds)
- [Azure Developer CLI templates overview](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-templates)
- [Bicep file structure and syntax](https://learn.microsoft.com/azure/azure-resource-manager/bicep/file)

### Coverage recovery gate: m01 at 20x

Rerun after the evidence-check coverage changes, using `.venv\Scripts\python.exe -m second_screen_pundit.agents.gate --live-latency --match m01 --speed 20`. The 15,000 ms live-window budget was unchanged. The shared-client warm-up took 10,404 ms total (2,788 ms for token acquisition and 7,437 ms for the model call), separately from the 297,888 ms replay.

| Path | Windows | Lines | Commentator time per emitted line | Whole-window time per emitted line | Fallback lines | Dropped lines |
| :--- | ---: | ---: | :--- | :--- | ---: | :--- |
| Must-say fast path | 8 | 8 | median 2,766 ms; slowest 4,531 ms | median 2,867 ms; slowest 4,672 ms | 0 | 0 |
| Tactical | 9 dispatched (30 boundaries) | 5 | median 2,664 ms; slowest 3,490 ms | median 8,531 ms; slowest 10,297 ms | 2 | 4 |

Tactical lines: 21' possession swing; 33' xG surge; 69' formation change to 4-2-3-1 (**deterministic fallback**); 75' formation change to 3-4-3 (**deterministic fallback**); 87' xG surge with a high-turnover surge. The 75' line explicitly says Ashbridge Rovers changed to a 3-4-3 shape, with Port Calder leading 2-1. The two formation windows failed the agent evidence check twice and were emitted from their verified deterministic facts with `source: "fallback"`. The other four tactical windows dropped because the Director declined twice and two model lines failed evidence validation twice (`CommentaryLine must cite an event`). No window timed out. All 8 fast-path lines were emitted by the Commentator; no Director or Analyst call was made on that route.

The 32 model calls all reported `finish_reason=stop`; no role truncated, so no token cap was raised. Role timings including retries: Director, 7 calls, median 2,891 ms, slowest 3,388 ms; Analyst, 11 calls, median 2,936 ms, slowest 4,967 ms; Commentator, 14 calls, median 2,813 ms, slowest 4,134 ms. Median input/output/reasoning tokens were Director 653/158/112, Analyst 1,270/133/0, and Commentator 894/225/86.5. The full test suite passed: 86 passed, 2 deselected.

## Phase 2c: voice and commentary style

Source for the Commentator's instructions and fallback variants: `prompts/commentator-persona.md`.

### Changes

- **Team names.** `engine/teams.py` defines one canonical full name per team (the squad name, for example "Port Calder FC") and one accepted short form (the same name without "FC"). Every analytics, console and signal fact label starts with the full name. The evidence check finds a label's team by either form, and accepts either form in text. Previously, signal labels used the short form and failed the full-name prefix check, so Port Calder's signal numbers and formations, including the deterministic fallbacks, were rejected.
- **Opponent and window labels.** Every tactical signal now carries an `_opponent` fact. The high-turnover and substitution-pressing window labels now contain a metric term the check recognises.
- **Match clock.** `match_context` facts "Match-level clock" and "Match-level minutes remaining" are added to every live run. A minute or time remaining is grounded only by these facts, in a sentence containing *minute* or *minutes left/remaining/to go/to play*.
- **Goal context.** Each goal gets a fact with one of: opener, equaliser, go-ahead goal, goal that extends the lead, consolation. "Consolation" means the scorer is still behind after the goal; the instructions say to treat it as "a goal back" live, because the final result isn't known.
- **Spoken values.** `Fact.spoken_value` holds a deterministic rounding (half up) for speech: whole numbers for `%` and percentage points, one decimal place for rates. xG never gets one. The evidence check accepts either the exact value or the spoken value.
- **One big-chance threshold.** xG ≥ 0.3 everywhere (`is_big_chance`): must-say shots, key moments and the "a big chance" descriptor. Previously must-say used > 0.25.
- **xG-surge descriptor.** A team-level `_chance_descriptor` fact ("the better of the chances", or "a string of clear chances" from 0.8 xG in the window). It contains no number.
- **Recent lines.** The dispatcher passes the last 3 emitted lines to the Commentator as `recent_lines`.
- **Instructions.** The Commentator instructions are rebuilt from the persona file: hard rules, goal context, formation wording, content limits and variety rules. The Analyst gets the formation-headline wording from the persona diagnosis.
- **Fallbacks.** `agents/fallbacks.py` holds the persona variants plus `booking` and `signal_red_card_formation`. A variant is chosen at random, seeded by the line ID, so replays are reproducible; a variant that fails the evidence check is skipped for the next. Non-mandatory signal windows now fall back too, once the Director has approved them.
- **A gap found by the tests.** When a red card and its reshape reach the same tactical window, the old fallback used only the first mandatory signal and dropped the formation. The combined `signal_red_card_formation` variants now carry both beats.
- **Delivery tier and SSML.** `CommentaryLine.delivery_tier` is set by the server from the event or signal; the model's value is overwritten. `speech.build_ssml` builds the document with the tier's style and style degree, XML-escapes every inserted value, and synthesis uses `speak_ssml_async`. The old error path called `SpeechSynthesisCancellationDetails.from_result`, which doesn't exist in the Python SDK; it now reads `result.cancellation_details`.

### Voice trial

Microsoft Learn's voice list (checked 2026-10-16) shows **no en-GB voice with `sports_commentary` or `sports_commentary_excited`**:

| Voice | Type | Styles listed on Learn |
| :--- | :--- | :--- |
| `en-GB-SoniaNeural` (current default) | Standard | cheerful, sad |
| `en-GB-RyanNeural` | Standard | chat, cheerful, sad, whispering |
| Other en-GB standard voices | Standard | none |
| `en-GB-AdaMultilingualNeural`, `en-GB-OllieMultilingualNeural` | Multilingual | none |
| `en-GB-Ada`, `-Ollie`, `-Ryan`, `-Sonia:DragonHDLatestNeural` | Neural HD | none (Dragon HD voices don't support `mstts:express-as`) |
| MAI voices | MAI | no en-GB MAI voice is listed |

The only voice on Learn listing the sports styles is `zh-CN-YunjianNeural`, which spells them `sports-commentary` and `sports-commentary-excited`.

Prices, from the Azure Retail Prices API (UK South, pay-as-you-go, USD): standard neural **$15** per 1M characters, Neural HD **$22** per 1M characters. No MAI meter is listed. The public pricing page renders its prices in script, so it showed none.

Method (`python -m second_screen_pundit.agents.gate --voice-trial`, output in `data/audio/voice-trials/`):
- The same 6 persona lines (2 goals, a red card, a substitution, a formation change, a tactical insight) are rendered with each voice: twice without a style and once with the tier's style.
- Two identical requests never return identical MP3 bytes, so bytes can't be compared. The evidence used is the audio duration the service reports.
- A styled render the same length as two equal plain renders means the style was ignored; a different length means it changed the delivery.
- The method only shows that delivery changed, not how it sounds; a listen is still needed.

| Voice | Result on the 6 lines |
| :--- | :--- |
| `en-GB-SoniaNeural` | Style **ignored** on all 6: durations identical to the plain renders |
| `en-GB-RyanNeural` | Delivery **changed** on 5 of 6 (formation line unchanged), although Learn doesn't list these styles for Ryan |
| `en-GB-OllieMultilingualNeural` | Style **ignored** on all 6 |
| `en-GB-Ollie:DragonHDLatestNeural` | **Unsupported voice** (error 1007) on this UK South resource |

Controls on one line: Ryan with its documented `cheerful` style changed delivery (7.61 s against 7.96 s plain). Yunjian changed delivery with **both** spellings (6.70 s against 7.34 s), so the underscore form used in the code is accepted. The resource also returned 429 Too Many Requests during the trial; the trial retries 429s only.

The default voice was not changed.

### Gates: m01 and m02 at 20x

Full suite: 106 passed, 2 deselected. Budget 15,000 ms per window, unchanged.

| Match | Path | Windows | Lines | Fallback lines | Dropped | Commentator per line, median / slowest | Whole run per line, median / slowest |
| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| m01 | Fast path | 8 | 8 | 5 | 0 | 7,195 / 7,938 ms | 7,336 / 8,282 ms |
| m01 | Tactical (30 boundaries) | 8 | 7 | 5 | 1 (Director declined) | 6,729 / 7,889 ms | 12,406 / 14,844 ms |
| m02 | Fast path | 5 | 5 | 2 | 0 | 4,632 / 8,367 ms | 4,750 / 8,500 ms |
| m02 | Tactical (30 boundaries) | 7 | 7 | 6 | 0 | 6,369 / 7,527 ms | 10,000 / 13,672 ms |

Every m01 and m02 beat was emitted, including m02's red card (39' tactical, 38' fast path) and 4-4-1 (39' fast path, 45' tactical). No window timed out.

**Regression: the Commentator's own lines often fail, and fallbacks carry most of the commentary** (m01 10 of 15 lines, m02 8 of 12). The fallback reasons were:

| Reason | m01 | m02 |
| :--- | ---: | ---: |
| Structured output failed on both attempts | 7 | 5 |
| …of which with `finish_reason=length` | 2 | 1 |
| Evidence check failed twice | 1 | 2 |

Commentator median tokens rose from 894 input, 225 output and 86 reasoning (coverage-recovery gate) to 1,807 / 368 / 162 (m01) and 1,849 / 383 / 183 (m02), against a 400-token cap that includes reasoning. The longer persona instructions, the extra prompt fields and the `delivery_tier` field in the response schema are the likely causes. The structured-output failures without truncation aren't diagnosed: the validation error isn't logged. Per-call Commentator median was 3,644 ms (m01) and 3,356 ms (m02), against 2,708 ms before. Lines ready after their event's release: 5 per match, at most 5,250 ms late.

Not fixed at this gate; candidates for the next step:
- raise the Commentator cap
- remove `delivery_tier` from the model's response schema, since the server sets it
- log the validation error
- shorten the instructions

Also seen in the emitted lines:
- **Weak agent lines that still pass the check:** "At 21 minutes, Ashbridge Rovers have 31 possession before and 53 after against Port Calder FC", and an m02 line containing a newline.
- **Formation changes heard twice:** the fast path announces them and the mandatory tactical signal repeats them (m01 67'/69' and 72'/75', m02 39'/45').

### Phase 2c regression fixes

**Changes:**
- **Reply schema.** The Commentator returns `CommentaryReply` (`text`, `fact_ids`, `event_ids`) only. The server sets `line_id`, `insight_id`, `tone` (from the delivery tier, `TIER_TONES`), `source` and `delivery_tier`. An over-long reply fails the evidence step with "text is N characters; the limit is 200" rather than failing schema validation.
- **Validation logging.** Every structured-output failure is logged (`agent_output_invalid`) with the failing fields and a 160-character excerpt of the reply, and listed in the gate report's `output_failures`. `finish_reason` is recorded per call and counted per role.
- **Cap.** Commentator `max_tokens` is 700.
- **Instructions.** `COMMENTATOR_INSTRUCTIONS` keeps the hard rules, goal context, formation wording and four persona examples (1, 5, 8 and 9). It went from 3,616 to 2,462 characters. The persona, energy tiers and variety rules stay in `prompts/commentator-persona.md`. One short rule is kept: don't open with the same words as a recent line.
- **Score rule.** `score_phrase` is supplied on goals and red cards, or when none of the last 3 lines said it, and is otherwise null. A line that states a score when the score was withheld fails the check. Fallbacks drop their closing score sentence in that case.
- **One line per formation change.** The dispatcher records each formation event the fast path announces, and the tactical lane skips that event's signal. The fast path now gets a previous-formation fact, so it can say "from X to Y". A new must-say fallback variant uses it.
- **Tidy text and percentages.** Whitespace and newlines are collapsed. A number grounded only by `%` facts must be followed by "%" or "per cent".
- **Default voice.** `en-GB-RyanNeural`, the only en-GB voice whose delivery changed under the sports styles in the voice trial.

**Gates at 20x** (15,000 ms budget):

| Match | Path | Windows | Lines | Agent | Fallback | Dropped | Run median / slowest |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- | :--- |
| m01 | Fast path | 8 | 8 | 8 | 0 | 0 | 3,938 / 6,547 ms |
| m01 | Tactical | 7 | 5 | 2 | 3 | 2 (Director declined) | 8,672 / 12,656 ms |
| m02 | Fast path | 5 | 5 | 3 | 2 | 0 | 5,391 / 6,328 ms |
| m02 | Tactical | 7 | 7 | 5 | 2 | 0 | 8,594 / 11,953 ms |

- **Agent share:** m01 10 of 13 (77%), m02 8 of 12 (67%), 18 of 25 overall (72%).
- **Structured-output failures:** none. Every call ended with `finish_reason=stop` (m01: Commentator 14, Analyst 8, Director 7; m02: 15, 9, 6). No call was cut off by the token cap.
- **Commentator tokens (median input / output / reasoning):** m01 1,656.5 / 285 / 113.5, against 1,807 / 368 / 162 before; m02 1,530 / 302 / 163, against 1,849 / 383 / 183 before. Median call time is 3,138 ms (m01) and 2,908 ms (m02).
- **Every fallback is now "evidence check failed twice".** In m01 all three are tactical windows where the Analyst's insight failed twice, so the Commentator never ran. In m02 two are fast-path goals (27' and 54') and two are tactical windows. Seven other windows passed on the Commentator's second attempt. The evidence-check failure text is not logged yet.
- **Beats:** all goals, the red card, the substitution and the formation changes produced a line. Each formation change was heard once. The m01 88' go-ahead goal was agent-written.
- **Viewer stream:** 2 lines per match were ready after their event's release, at most 3,094 ms late (m01) and 2,219 ms late (m02), against 5 per match and 5,250 ms before.

**Targets:**
- m02 misses two of them: its agent share is 67% (target 70%) and its fast-path median is 5,391 ms (target under 4 s). Its two fallback goals each cost two Commentator calls.
- m01 meets all of them, but its fast-path median of 3,938 ms is only just under 4 s.

**New in the emitted lines:** with the variety rules out of the prompt, the model states the minute in most lines, as "At N minutes" or "at N minutes" (8 of 10 agent lines in m01, 6 of 8 in m02), and sometimes adds "N minutes left". At 74' in m01 it used the word "consolation" for a goal that pulled one back mid-match.

## Phase 3: review 2 fixes

Scope: findings 1, 2, 3, 4 (items 1–4), 6 (prompt and ID validation) and 15 of `docs/reviews/review-2-opus.md`.

### Changes

- **Viewer snapshot.** `match_snapshot` computes, at the viewer's released position, the clock, score, both formations, per-team possession (whole match and the last 15 minutes), shots, shots on target, xG, cards and players on the pitch, plus the last 5 key moments. Each moment has a plain description (scorer, goal context, chance quality, running score, previous and new formation) and is its own `ToolResult`, so it keeps its event ID under the five-ID cap. The Explainer prompt carries this snapshot, `viewer_clock` and a deterministic `score_phrase`.
- **Explainer reply.** The model returns `ExplainerReply { answer, fact_ids }` only. Facts are shown to it as short aliases (`F1`…), including tool results (the capture middleware restores the real IDs). The server maps aliases back, derives event IDs from the cited facts, and lists the cited facts as deterministic claims. `xg_timeline` is not offered to the Explainer.
- **Snapshot facts are cited by the server.** Every snapshot fact given to the Explainer or the Recap Producer is cited for its output, as the live Analyst path does for the score. The spec grounds a number in "this run's tool results", and the models listed the facts they used unreliably: in live runs an answer stated the score and previous formation without their IDs, and a recap quoted shot counts and xG without theirs. The number check is unchanged: a matching value, with the team and metric named in the same sentence. Facts from tool calls the model makes must still be cited by the model.
- **Caps and budget.** `reasoning.effort: low` for both. Explainer `max_tokens` 1,500; Recap 2,500 (one recap hit 1,500 and stopped with `finish_reason=length`; a later one used 1,576). `finish_reason` is logged per call and returned in each answer payload. `EXPLAINER_TIMEOUT_SECONDS` is 20 and `LIVE_WINDOW_TIMEOUT_SECONDS` 15 again (the spec values). An exception now retries once, with the parse error as feedback.
- **Text before audio.** `/api/ask` returns the checked text at once (no TTS, as before; *changed in Session B, see "App shell": typed answers are now spoken too*). `/api/ask-audio` returns `202 {question_id, transcript}` as soon as speech-to-text finishes; the answer follows on the replay's SSE stream as `answer`, then its audio as `answer_audio` (or `answer_audio_unavailable` / `answer_error`). The 30-second limit covers STT plus the answer.
- **Out of scope.** A question with betting terms or a denylisted real name gets a fixed reply with no model call.
- **Content filter.** It blocks only the hashed real-name denylist (possessives folded) and betting terms ("odds", except "against the odds"/"against all odds"; "stakes" no longer matches). The capitalised-word heuristic is gone. It runs only on the model-written answer, never on notices or deterministic fact text. Viewer-facing notices and errors no longer contain exception class names.
- **Formation reset.** The 46' kick-off carries no formation, and `_formation_at` reads only the period-1 kick-off, so stale event logs are also correct. m02's home formation at 60' is 4-4-1.
- **Recap.** The prompt carries a winner-first `score_phrase` ("Merehaven Athletic win 2-1"), the clock facts, the stats and every turning point (goals, red cards, formation changes, substitutions) with minute, event IDs, description and running score. Every ID the model returns is checked against the listed facts and the released events: an event ID among `fact_ids` (or an F ID among `event_ids`) is moved to the right list, and any other ID gets retry feedback naming it. Evidence failures in Q&A and the recap get a one-line fix hint per failure kind. The instructions limit statistics and say how to tell a comeback.
- **Evidence check.** For answers and recaps only, a minute is grounded by the clock of a cited event (the spec's "or the minute of a cited event") or by a minute in a cited fact's label, such as its window `59-75'`; live lines keep the clock-fact rule. A score never ends in `'`, so `59-75'` is no longer read as a score. "men" counts as the players metric.

### Live gate: m02 at 20x, the review's 8 questions

Same questions and viewer clocks as review 2, final code. Time to text runs from the request to the checked answer text; for the spoken question, to the `answer` SSE event, with the transcript returned first.

| Viewer clock | Question | Source | Attempts | finish_reason | Time to text |
| :-- | :-- | :-- | --: | :-- | --: |
| 15' | Who has had the better of the chances so far? | model | 1 | stop | 4.3 s |
| 30' | How did Port Calder score their goal? | model | 1 | stop | 3.0 s |
| 42' | spoken: What changed for Port Calder after the red card? | model | 1 | stop | 6.3 s (transcript 3.4 s, audio 8.7 s) |
| 50' | What changed for Port Calder after the red card? | model | 1 | stop | 3.4 s |
| 58' | What are the odds Merehaven win this? | fixed reply | 0 | – | 0.004 s |
| 66' | Is Merehaven's pressing better than Liverpool's? | fixed reply | 0 | – | 0.006 s |
| 74' | Why are Merehaven camped in the final third? | model | 1 | stop | 3.2 s |
| 84' | What formation are Port Calder playing now? | model | 1 | stop | 2.5 s |

6 of 8 got a model answer (target: at least 6); the other 2 are out of scope by design. Explainer calls: 2.4–4.2 s, input 1,767–2,191 tokens, output 106–231 (32–134 reasoning). The 84' answer: "Port Calder FC are now set up in a 4-4-1, having changed from a 4-2-3-1 in the 39th minute."

The recap was agent-written on its second attempt (the first failed on an ungrounded "19"; calls 13.6 s and 9.8 s, `finish_reason=stop`):

> **Merehaven rally late to seize the points.** Port Calder FC led through Brio Fallowmere's opener in the 27th minute, then Quen Rillstone's red card in the 38th minute forced a reshuffle to 4-4-1. Merehaven Athletic struck back in the 54th minute and completed the turnaround in the 82nd, and Merehaven Athletic win 2-1. Port Calder FC had 19 shots and 7 on target. Merehaven Athletic had 33 shots and 3.919 xG.

**Earlier runs while fixing.** Run 1 (headline facts not auto-cited): 5 of 8; the 50' answer stated the score and previous formation without citing them. Run 2 (headline facts auto-cited): 6 of 8, recap agent-written. Run 3 (after adding running scores and the recap story rule): 5 of 8 and a fallback recap; the 74' answer quoted the window `59-75'`, which the check read as a score, and both recap attempts used uncited snapshot stats. Run 4 is the table above. Separately, 8 standalone recaps after the recap changes: m01 3 of 3 agent-written, m02 4 of 5 (the miss was the `length` cut-off that led to the 2,500 cap), m03 1 of 1.

Full suite: 170 passed, 3 deselected (live). The live `test_live_m01_recap_is_agent_written` passed.

## Phase 3 Session B, group 1: what the viewer hears

### Voice: en-GB-Harry (MAI-Voice-2.1-Flash), Ryan as fallback

Evidence from 17 Oct 2026:
- **UK South rejects MAI voices.** Error 1007, "Unsupported voice … aoai-tts-servercontroller".
- **No en-GB sports style.** The UK South voice list has no en-GB voice with a sports-commentary style; Ryan has only cheerful and chat.
- **Sweden Central serves Harry.** The new resource `pundit-speech-swc-kl` serves `en-GB-Harry:MAI-Voice-2.1-Flash`, and in a listening check its styles clearly change the delivery. Learn's MAI-Voice page lists Sweden Central among the serving regions, and lists Harry's styles as agent, angry, audiobook, customer_call_center, disgusted, educational, fearful, joyful, narrator, neutral, sad and surprised. Learn documents no `styledegree` for MAI voices, so none is sent.
- **Speed.** `python -m second_screen_pundit.agents.gate --voice-trial`, 7 lines × 3 requests per voice, wall clock per request: Harry's first request took 4,308 ms, then a median of 2,428 ms per line (slowest 3,974 ms, 20 requests). Ryan's first took 3,222 ms, then a median of 2,496 ms (slowest 4,152 ms).
- **Ryan works on the same resource.** All 21 Ryan requests succeeded on the Sweden Central endpoint, so both voices use the one `SPEECH_ENDPOINT`.
- **The duration method can't prove Harry's styles.** Harry's two plain renders of the same line differ in length on all 7 lines, so the trial reports "inconclusive" for Harry; the evidence that his styles work is the listening check. On Ryan, cheerful and chat changed the duration on all 5 styled lines.

**Settings.** `SPEECH_VOICE` (default `en-GB-Harry:MAI-Voice-2.1-Flash`) and `SPEECH_FALLBACK_VOICE` (default `en-GB-RyanNeural`). If the main voice fails, synthesis retries once with the fallback and logs `speech_voice_fallback`; every success logs `speech_synthesized voice=…`. The text is always sent, whatever happens to the audio.

**Styles per voice** (`agents/delivery.py`, `VOICE_PROFILES`). A style is sent only if the voice's Learn list includes it, and a voice with no profile gets no style. The old `sports_commentary` map is gone: neither voice lists it.

| Tier | Harry | Ryan |
| :-- | :-- | :-- |
| Goal, equaliser | joyful | cheerful (1.2, 1.5) |
| Late go-ahead goal (75'+), big chance, red card | surprised | goal: cheerful 1.7; others none |
| Formation change, tactical | narrator | chat 1.0 |
| Substitution, booking, routine | neutral | none |

There are no penalty events in the data, so penalties have no tier yet.

### Spoken text

`agents/spoken.py` `to_spoken`, called only in `SpeechSynthesizer.synthesize_mp3`; displayed text, the evidence check and stored lines are unchanged. The rules follow review 2 finding 8, with British phrasing: "one-nil", "nil-nil", "one-all" for any level score, "four-two-three-one", "in the eighty-eighth minute" (minute marks and "At N minutes" become word ordinals; so does a digit ordinal before "minute", which the review left unchanged), "ninety plus three", "60 to 75 minutes", "between 60 and 75 minutes", "per cent". "N minutes left" and xG are unchanged. The tests check idempotence and that no dashed number pair survives.

### Audio queue

- **Server.** The dispatcher's `on_line_ready` hook starts TTS as soon as a line passes its checks, while it waits out the broadcast delay. If the audio is ready at release, the `commentary` event carries `audio_id`; otherwise the text goes at once and `commentary_audio` follows.
- **Browser.** One `Audio` element, unlocked with a silent clip inside the Start click. Lines play in order with no per-line player. At most one line waits behind the one playing, and a line's audio is dropped if it hasn't started 12 s after its text arrived. Answers, the recap and "▶ Replay" go to the front without cutting off the current line. Mute clears the queue and is kept in `localStorage`. Pause and Resume also pause and resume the queue. If the unlock failed, an "Enable audio" button appears.

## Phase 3 Session B, group 2: trust and accuracy

### Q&A citations

Phase 3 cited every snapshot fact for every answer. Now an answer cites only:
- the facts from the model's own tool calls that it listed, and
- for each number, score, formation or minute in the answer, the snapshot fact the evidence check matched to it. If the model listed one of the matching facts, that one is used. The check now returns this as `EvidenceReport.grounding`.

How it works: the answer is first checked against every snapshot fact, as before, so the pass rate doesn't change. Then the citations are narrowed to the facts that were used, and the narrowed answer is checked again. If the narrowed answer ever failed, the passing answer would be kept and `question_citation_narrowing_failed` logged; no test produces this. The claims list and event IDs follow the narrowed facts. An answer that uses one stat cites exactly that fact, whatever the model listed, and an answer with no numbers cites nothing. Most specific mentions are handled first, so a minute window such as `59-75'` is covered by the fact already kept for its number rather than adding the other team's fact.

### Recap

- **No raw xG.** The recap prompt no longer carries xG facts. A new deterministic `chance_summary` gives each team's big-chance count, its xG rounded half up to whole expected goals ("about 4 expected goals"), and "the better of the chances" for the team with more xG. The check fails any recap text with an xG decimal or any number with two or more decimal places, and the retry says what to write instead.
- **Headline.** The server rejects a headline without the winner-first `score_phrase` and retries with it named.
- **Fallback recap.** It is written from the full-time snapshot. The headline is "Merehaven Athletic win 2-1 at Saltmere Park". The summary tells every red card and the latest goals in order, from each moment's description, then "Merehaven Athletic had the better of the chances, with about 4 expected goals." Key moments are the red cards, then the latest goals. "The consolation" becomes "a goal back" at full time, since the team may have come back (m01). It passes the evidence check on all three matches.
- **Cached recaps.** Headlines reworded to include the score phrase ("Ashbridge Rovers win 3-2 with a late comeback").

### Timing

- **No line before its event** (review 2 finding 9). `publish_outcome` waits on the session's `release_condition` until the viewer stream has released the line's `as_of` event. A test that slows the viewer loop fails without this wait.
- **Pause is immediate** (finding 10). `BroadcastDelay.pause()` holds every release, events and lines alike. `resume()` pushes each unreleased release time back by the pause (for an event that arrived mid-pause, by the rest of the pause), so the 10 s delay is kept. `/control` calls them when the replay's state becomes paused or running. Arrival stamps are unchanged, so latency figures don't change.

## Phase 3 Session B, group 3: robustness and security

### Replay slots

- **Stream deadline.** `/start` starts a watchdog. If no stream connects within `STREAM_CONNECT_DEADLINE_SECONDS` (default 10), the replay is stopped and its slot freed (`replay_stream_deadline` is logged). Opening the stream cancels the watchdog. Before this, three starts with no stream held all three slots and kept making model calls.
- **One replay per client.** A client is its IP address (below). A new `/start` from a client stops that client's earlier replay instead of returning 409. A lost stream or a closed tab therefore never locks a client out, and one client can't hold every slot by sending different `X-Tab-ID`s. Two viewers behind one NAT address share a single replay at a time; that's the price of a per-client limit.

### Client IP

`_client_ip` uses the **right-most** `X-Forwarded-For` entry. Learn's Container Apps ingress page says the header "is appended to. Only the rightmost IP is provided by Azure Container Apps. Any other values must be validated by the user to prevent IP spoofing." The question limiter and the per-client replay limit both use it, so a client that rotates a spoofed prefix stays one client.

### Event loop

`LiveDispatcher` runs each 3-minute boundary's signal evaluation and window scan in `asyncio.to_thread`. Only the boundary coroutine touches the tracker, and it is awaited before the next event is read, so the thread never races the loop. Longest gap between ticks of a 5 ms event-loop ticker while the dispatcher runs a whole match (fake pipeline, source yielding per event):

| Match | Before: longest / p99 | After: longest / p99 |
| :-- | :-- | :-- |
| m01 | 814 / 440 ms | 86 / 34 ms |
| m02 | 842 / 445 ms | 95 / 29 ms |
| m03 | 481 / 410 ms | 81 / 30 ms |

The rest of the gap is GIL contention with the worker thread plus the per-event must-say check. `tests/test_robustness.py` fails on the old dispatcher (251 ms with a 0.25 s boundary; 726 ms over m02) and passes on the new one.

### Phase 4 container checklist (not built yet)

Added to review 2's "Ready to deploy (Phase 4)" table:
- **Speech role.** The app's user-assigned managed identity needs **Cognitive Services Speech User** on the Sweden Central Speech resource (`pundit-speech-swc-kl`). Assign it in Bicep, scoped to that resource, not the resource group. `SPEECH_ENDPOINT` and `SPEECH_REGION` point at Sweden Central; `SPEECH_VOICE` and `SPEECH_FALLBACK_VOICE` are passed through.
- **Ingress request timeout.** Learn's ingress page lists "Request time out is 240 seconds" for HTTP ingress. A 20x replay's stream lasts about 5 minutes, and a 1x stream about 95. Check on Learn whether this limit applies to a streaming response with 15 s heartbeats before Phase 4 relies on one long SSE request.

### Session B final gate, 17 Oct 2026

The README server, with INFO logs to a file, on port 8001. Run 1 was in a Playwright Chromium page, with Start pressed by a real click and a recorder on the page's SSE handler and audio element. Runs 2 and 3 used a scripted SSE client. All at 20x. `speech_synthesized` now logs the style sent.

| Check | Run | Result |
| :-- | :-- | :-- |
| Lines no earlier than their events | m01 (13 lines), m02 (12 lines) | 0 lines arrived before an event they cite |
| Harry audio plays automatically after Start | m01, browser | 13 of 13 line clips played with no click after Start, in line order, then the recap clip; no "Enable audio" button. 8 of 13 lines arrived with their audio in the same event; the rest got `commentary_audio` afterwards |
| Goal, red card and tactical line styles | m01, m02 | Goals and the equaliser `joyful`; the 88' (m01) and 82' (m02) late go-ahead goals `surprised`; the m02 red card `surprised`; tactical `narrator`; formation `narrator`; substitution `neutral`. Every line used Harry |
| Bad voice falls back to Ryan | m01, `SPEECH_VOICE=en-GB-Nobody:MAI-Voice-2.1-Flash` | Each request failed with 1007, was logged as `speech_voice_fallback` and retried once with `en-GB-RyanNeural` (tactical `chat`, goal `cheerful`); every line's text was delivered |
| Recap with no decimals | m01, m02 | Agent-written on the first attempt in both. No text matches `\d\.\d{2,}`. Headlines: "Ashbridge Rovers win 3-2 after stunning late turnaround", "Merehaven Athletic win 2-1 after late surge" |

m01 has no red card, so the red-card style was checked on m02.

**Seen in the lines, not fixed here:**
- **Red card heard twice.** In m02 the fast path and the mandatory tactical signal each produced a red-card line, 0.3 s apart (review 2 finding 14).
- **Minutes still spoken as numbers.** Agent lines still say "At 21 minutes" and "24 minutes left". Spoken text reads them correctly, but they're repetitive.
- **Missing full stop.** One line ended "The score is level at 0-0" without one.

## Phase 3 Session B: app shell

### Layout

- **One fixed screen.** The page no longer scrolls. The shell fills the viewport height (`100dvh`). The scoreboard and controls stay at the top, only the feed scrolls, and the ask bar is pinned beneath the feed.
- **Breakpoint at 1024px.** From 1024px wide, two columns: match info on the left (about 40%: pitch mini-map, formations and stats, key moments) and the feed on the right (about 60%). Below 1024px, one column, with match info in a collapsible "Match info" drawer that opens over the feed. 1024px is the narrowest width at which a 40% column still fits the pitch and the stats table side by side with a readable feed.
- **Key moments scroll inside their own box.** The list grows all match and can't fit beside the pitch and stats on a 768px-tall screen, so it is the one scroller outside the feed. On short desktop screens (860px tall or less) the pitch shrinks, so about five moments stay visible at 1366×768.
- **Feed.** Newest at the bottom. It follows the newest line only when the viewer is within 80px of the bottom; otherwise a "↓ New lines" pill appears. The "following" state survives resizes, the drawer and the keyboard.
- **Q&A in the feed.** "YOU ASKED", then "Thinking…", replaced by the answer card with Why? and Replay. An answer's Why? is built from its own payload and the events the page has already received, so it needs no API call.
- **Mini-map.** An SVG pitch showing the latest event's position, and its pass or carry end, in the team's colour. Away coordinates are flipped for display, as the spec says. It uses event data only.
- **Shortcuts.** `/` focuses the ask box (not from inside another text field). Enter sends; Shift+Enter starts a new line.
- **Crowd.** A disabled toggle marked "soon"; there is no crowd audio.

### Phone keyboard

- **Android Chrome.** The viewport meta has `interactive-widget=resizes-content`, so the layout viewport shrinks above the keyboard and the `100dvh` shell shrinks with it.
- **iOS Safari.** The keyboard overlays the page and only the visual viewport shrinks. `app.js` listens to `visualViewport` resize and scroll and sets the shell height to `visualViewport.height`, translated by `offsetTop`, so the ask bar sits on the keyboard's top edge.
- **How it was checked.** Both cases were checked in Playwright Chromium: a 390×510 viewport with the box focused (Android model), and a stubbed 510px visual viewport inside an 844px page, with and without a 40px `offsetTop` (iOS model). The ask bar ended at the keyboard edge each time. No real device has been tested yet.
- **Screenshots** are in `docs/screenshots/app-shell/`, taken against the current code in cached mode.

### Typed answers are spoken (changes the Phase 3 note)

`/api/ask` still returns the checked text at once, now with `audio_pending: true`. It then synthesises the answer in the background and sends `answer_audio` (or `answer_audio_unavailable`) over the replay's SSE stream, with the same `question_id`, exactly as a spoken question does. The page plays it through the same audio queue, so it follows Mute and jumps the queue like a spoken answer, and the answer card gets a Replay button.

### Cached mode published lines twice (fixed)

Cached mode is what viewers get when the spending guard switches the site to `NARRATIVE_MODE=cached`.
- **Cause.** For each window, cached mode picks the first pre-generated line that cites any event in the window. Two windows can match one line: a goal's fast-path window, and the next tactical window, which holds the goal's linked shot (the shot is not must-say, so it falls into the tactical window). Nothing recorded which lines had gone out, so the line was published twice under one `line_id`. A dry run published 11 lines for 8 distinct in m01 (3 repeated), 7 for 5 in m02 (2 repeated) and 4 for 3 in m03 (1 repeated).
- **Second problem.** The dispatcher treated only status `"commentary"` as a line, and cached results have status `"cached"`. Cached lines therefore skipped early audio, the broadcast-delay bookkeeping and the recent-lines memory. With both copies sharing one ID, the page attached audio to the first card only, so the second stayed on "Preparing audio…".
- **Fix.** The dispatcher counts both statuses as lines, and keeps the set of published `line_id`s; a repeat becomes `skipped`. `tests/test_cached_mode.py` checks that each line is published once on all three matches, and that every cached line gets early audio. Both checks failed before the fix.

## Phase 4, group 1: the stream survives the ingress timeout

### The limit

Learn's ingress page ("Ingress in Azure Container Apps", HTTP protocol) says "Request time out is 240 seconds", and the default ingress idle timeout is 4 minutes. It doesn't exempt streaming responses, so the design assumes ingress cuts any request at 240 s, heartbeats or not. A 20x stream lasts about 5 minutes, so one long request can't carry a match.

### Resume, not restart (changes B3 and "Out of scope")

The spec said a replay stops when its SSE client disconnects, with no resume, and listed resuming as out of scope. Phase 4 needs resume, so that changes:
- **Outbox.** Everything a replay sends its viewer goes into an append-only `ViewerOutbox` (`outbox.py`), kept for the life of the replay. A message's SSE `id:` is its 1-based position. Each connection starts with `retry: 1000`.
- **Reconnect.** `GET /api/replays/{id}/stream` reads `Last-Event-ID` and sends every message after it, in order: nothing the page already has, nothing skipped. A spoken answer or line audio that was still being made when the stream dropped lands in the outbox when ready, so the reconnected stream sends it once.
- **A drop doesn't stop the replay.** It keeps running (or stays paused) for `STREAM_RECONNECT_GRACE_SECONDS` (default 30). If no stream reconnects in that time, it is stopped and its slot freed, as before (`replay_stream_not_reconnected` is logged). The first-connect deadline is unchanged.
- **Unchanged by a reconnect:** pause state and the broadcast delay (both live on the replay, not the connection), the viewer cut-off for Q&A (`viewer_released_as_of`), the one-replay-per-client rule (reconnect isn't `/start`), and the page's audio queue (the page isn't reloaded; only the stream reader reconnects).
- **Newest connection wins.** A reconnect often arrives before the server has noticed the old socket is dead. The old stream ends at once instead of the new one getting a 409.
- **Safety net.** Each connection closes itself cleanly after `STREAM_MAX_SECONDS` (default 200), inside the 240 s limit, and the page reconnects with `Last-Event-ID` a second later. Heartbeats stay at 15 s.
- **Page.** The stream reader is `fetch`-based, not `EventSource`, so it does the reconnect itself: it records each message's `id` after handling it, honours `retry`, sends `Last-Event-ID`, and stops on a 4xx (for example 404 once the replay has gone). After full time it keeps reconnecting, so line audio and evidence stay available while the tab is open, as before; a finished replay holds no slot.

### Tests

`tests/test_stream_resume.py`, all simulated in-process (closing the response body is the drop):
- m01 in cached mode, dropped after two lines and reconnected 0.5 s later: message IDs run 1..N with no gap or repeat, every event arrives once in order, every line once, and no line before an event it cites. Messages were sent while the page was away, so the resume is real.
- Dropped while a spoken question's answer is held: the `answer` and its `answer_audio` arrive once, after the reconnect, and the answer used the viewer cut-off from before the drop.
- Dropped while paused: after reconnecting nothing new arrives, the replay and the delay are still paused and the cut-off hasn't moved; Resume continues the IDs with no gap.
- `STREAM_MAX_SECONDS=0.5`: three connections each closed by the server, the replay running throughout, IDs contiguous across all three.
- A new connection ends a stale one that is waiting for its next message.

Mutation checks: with the server ignoring `Last-Event-ID`, the first four fail. With no reconnect grace (a drop stops the replay, as before), the mid-match, spoken-answer and paused tests fail.

**The page's reconnect, in a browser.** Local server, cached mode, `STREAM_MAX_SECONDS=15`, m01 at 60x in Playwright Chromium, Start clicked. The page opened 9 connections; each reconnect sent `Last-Event-ID` (60, 291, 511, …, 1413). It received 1,548 events, each once and in order; 8 lines, 8 cards and 8 line clips queued, none twice; then one recap and one recap clip, and "Replay complete". That's the client code the unit tests can't reach. The deployed gate repeats this at 20x across the real 200 s close.
