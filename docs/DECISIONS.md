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
