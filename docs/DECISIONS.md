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
- The browser reports whether speech is playing or queued through the replay-token-protected `/playback-state` endpoint. Playback-state revisions make late requests harmless.
- **Crowd bed.** Four low-mid, band-limited and independently modulated noise layers generate the browser-only ambience at about half the previous base level; no asset or server call is used. Start unlocks it, its on/off setting is kept in `localStorage`, and it follows Mute. Commentary pre-ducks it by 12 dB starting 300 ms before playback, holding until 400 ms after; goals swell it for about three seconds (more from 75'), and big chances and red cards give a smaller lift, all hidden under speech. Pause and page hide suspend it; the Welcome back tap resumes the context inside the gesture. Short, moderate-volume Web Audio whistles mark each half's kick-off, half-time (two short blasts), and full time (two short, one long); Mute and Crowd-off silence them.

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

### Follow-ups after group 1

- **A replay belongs to whoever holds its token** (replaced an IP-match check from earlier in group 2, which broke viewers sharing an office address and phones that change network).
  - `/start` returns `replay_token` (`secrets.token_urlsafe(24)`, 192 bits) with the replay ID. The page keeps it in memory only and sends it as `X-Replay-Token` on the stream, `/control`, `/api/ask` and `/api/ask-audio`. A missing or wrong token gets the same 404 as a missing replay (constant-time compare). The caller's address plays no part, so a reconnect from a new network resumes.
  - **One replay per viewer**, keyed on `X-Client-ID`: an ID the page makes once and keeps in `sessionStorage` (it replaces `X-Tab-ID`). A viewer's new Start stops only their own earlier replay; two people on one network keep theirs.
  - **The address is for limits only:** the question rate limit, and at most 3 *running* replays per address (`429` beyond that), inside the global cap (3 then; 6 since the gate prep below). Rotating the client ID or a spoofed `X-Forwarded-For` prefix doesn't escape the per-address cap, since the address is the right-most entry.
  - Tests: without or with a wrong token, every replay call is refused (fails with the check removed); two client IDs on one address each run a replay and a third is allowed, a fourth gets 429; a reconnect from a different address with the token resumes with contiguous message IDs.
- **Memory.** When a replay is stopped (a new Start from the same client, or no reconnect within the grace period) its session is removed, its audio cleared and its outbox emptied. A replay that has reached full time is kept while its tab stays open, so audio Replay and Why? still work; it holds no replay slot. Measured for m01 in cached mode: 1,567 outbox messages, 623 KB of JSON, about 4.5 MB of Python heap with stub audio. Real audio is 160 kbit/s MP3, about 20 KB per spoken second: about 2–3 MB of line audio and 1 MB of recap for a 20x match. Spoken answers add about 200 KB each, so the worst case is set by the question limit (6 a minute) times the match length; the group 3 TTS cap bounds it.

## Phase 4, group 2: infrastructure

### Where it deploys

- `infra/main.bicep` is resource-group scoped (azd's resource-group-scoped deployment, beta on Learn). It creates no resource group. The `preprovision` hook in `azure.yaml` sets `AZURE_RESOURCE_GROUP` to `rg-<environment name>` when unset and fails if that group doesn't exist, so environment `pundit` deploys into the existing `rg-pundit` and azd never offers to create a group.
- `environmentName` is used only for the `azd-env-name` tag and the resource token (`uniqueString(resourceGroup().id, environmentName, location)`).
- Region: UK South, a template default that doesn't follow `AZURE_LOCATION`. The model stays in Australia East (the existing Foundry account) and Speech in Sweden Central.
- `.azure/` is in `.gitignore`.
- **Checked before deploying:** `azd provision --preview` for `pundit` with `AZURE_RESOURCE_GROUP` unset. The hook set it to `rg-pundit`. The plan creates the Container App, its environment, the registry, Application Insights and Log Analytics in `rg-pundit`, and skips every existing resource.

### Replicas (decision)

- **`maxReplicas: 1`.** Streams, outboxes, replays, pause state and resume positions live in one process's memory. A second replica would receive a reconnect it knows nothing about and break resume. One uvicorn worker, for the same reason.
- **`minReplicas` is a parameter (`MIN_REPLICAS`), default 1**, so the app stays warm for judging: no cold start, no warm-up page for the first judge. Set 0 to scale to zero outside judging. A restart or new revision ends every replay.
- 0.5 vCPU and 1 GiB.

### Identity and roles

One user-assigned managed identity; `AZURE_CLIENT_ID` points `DefaultAzureCredential` at it. Roles by GUID (Learn's built-in roles pages), each scoped to its resource:

| Role | GUID | Scope |
| :-- | :-- | :-- |
| Foundry User | `53ca6127-db72-4b80-b1b0-d745d6d5456d` | The existing Foundry account (`existingFoundryAccountName` in `existingFoundryResourceGroup`) |
| Cognitive Services Speech User | `f2dc8367-1007-4938-bd23-fe263f013447` | The existing Speech resource `pundit-speech-swc-kl` |
| AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | The registry |
| Monitoring Metrics Publisher | `3913510d-42f4-4e42-8a64-420c390055eb` | Application Insights |

The Foundry account, its project and the Speech resource are `existing` references: a missing one fails the deployment, and none is recreated. B7's other path (create a new Foundry account when the names are empty) isn't built; the names are required.

### Settings

Plain container environment variables, no secrets and no keys: `NARRATIVE_MODE`, `FOUNDRY_PROJECT_ENDPOINT` (built from the account's subdomain and project), `MODEL_FAST`, `MODEL_DEEP`, `SPEECH_ENDPOINT` and `SPEECH_REGION` (read from the Speech resource), `SPEECH_VOICE`, `SPEECH_FALLBACK_VOICE`, `AZURE_CLIENT_ID`, `APPLICATIONINSIGHTS_CONNECTION_STRING`, `OTEL_SERVICE_NAME`, `ENABLE_SENSITIVE_DATA=false`, `PORT`. The App Insights connection string contains an instrumentation key, which identifies the resource rather than authorising anything: with local auth disabled, ingestion needs an Entra token.
- The registry has the admin user off; the app pulls with the identity.
- The Container Apps environment sends logs through Azure Monitor diagnostic settings to Log Analytics, so it holds no Log Analytics shared key.
- `.dockerignore` excludes `.env`, `.venv`, `.azure`, `.git`, `docs` (including `docs/screenshots`), `.playwright-mcp`, tests, prompts and infra.

### Image

`python:3.12-slim-bookworm` (Debian 12 is on the Speech SDK's supported list) with `ca-certificates`, `libssl3` and `libasound2`. Non-root user. `WORKDIR /app` with `data/cached` copied in, so the cache path (relative to the working directory) resolves; matches are generated in memory and nothing is written at runtime except temp audio under `/tmp`. `third_party/` is copied so the MIT licence of the adapted grounding code ships with it. Entry point `python -m second_screen_pundit.serve`: logging, then Azure Monitor, then the app, on `0.0.0.0:8000` with one worker.

### Health

`/healthz` reads in-process state only; a test fails if it touches the pipeline or speech. It answers 200 while warming, so startup and readiness don't wait for warm-up and the page can show its warming state. Probes: startup every 3 s for up to 60 s, liveness every 30 s, readiness every 10 s. On the very first provision the app runs a placeholder image with no custom probes until azd deploys the real image (`SERVICE_API_IMAGE_NAME`). The ingress target port is 8000 from the start: `azd deploy` swaps only the image and keeps the port. The first deployment used the placeholder's port 80, and the real revision sat in "Activating" behind it; that's fixed.

### Observability

- `configure_azure_monitor(connection_string=…, credential=ManagedIdentityCredential(client_id=AZURE_CLIENT_ID))`, keyless, with Application Insights `DisableLocalAuth: true`. `azure-monitor-opentelemetry==1.8.11`. App logs under `second_screen_pundit` go to App Insights as traces.
- Agent Framework 1.22 emits `invoke_agent`, `chat` and `execute_tool` spans by default once a tracer provider exists; no GenAI spans are made by hand.
- `pundit.turn` wraps each live window, question and recap, with `app.route`, `app.match_id`, `app.as_of`, `app.evidence.status`, `app.outcome` and token totals. `pundit.speech.synthesize` and `pundit.speech.recognize` wrap the speech calls (characters or bytes, delivery tier). No question or answer text goes on a span.
- No request spans for `/healthz`, `/api/audio/` or the SSE stream (`OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`). The stream alone added one "http send" span per message, about 1,500 a match, and every probe added two.
- The Azure SDK logger is set to WARNING; at INFO it logged every telemetry export's HTTP headers.
- **Not yet:** `app.estimated_cost_usd` needs `agents/pricing.py` (dated list prices), which isn't built.
- **Open (existing code):** the app's own `agent_call` log and the `pundit.turn` token totals read `usage_details` from the workflow response and get `None` in Azure. The `chat` spans carry the real counts (`gen_ai.usage.input_tokens` and `output_tokens`).

### Deployed, 17 Oct 2026

`azd` environment `pundit`, settings `EXISTING_FOUNDRY_ACCOUNT_NAME=second-pundit-resource`, `EXISTING_FOUNDRY_RESOURCE_GROUP=rg-pundit`, `FOUNDRY_PROJECT_NAME=second-pundit`, `EXISTING_SPEECH_ACCOUNT_NAME=pundit-speech-swc-kl`, `MODEL_FAST`/`MODEL_DEEP=gpt-5.4-mini`; `AZURE_RESOURCE_GROUP` set by the hook.
- `azd up` succeeded (remote ACR build; 5 min 22 s the first time, 2 min 10 s after). `az group list` shows `rg-pundit` and no other `pundit` group.
- The app: target port 8000, three probes, 0.5 vCPU, 1 GiB, min 1, max 1. `secrets` is null; the registry entry has an identity and no username or password; the environment's logs destination is `azure-monitor` with no Log Analytics configuration; the registry's admin user is off; App Insights `DisableLocalAuth` is true.
- `/healthz` reported `warmup: ready`, so the identity's Foundry call worked.
- Smoke test: m01 at 120x from a script. The first line arrived 22.6 s in, its audio followed as `commentary_audio`, and `/api/audio/{id}` returned a 176,640-byte `audio/mpeg`. When the script left, the replay stopped 30 s later (`replay_stream_not_reconnected`).
- App Insights, last 30 minutes: `chat gpt-5.4-mini` and `pundit.turn` dependencies, `GET /msi/token` (keyless ingestion and model access), and the app's logs as traces.

## Phase 4, group 3: public-facing safety and first impressions

### Spending guard

- **Caps.** `MODEL_CALLS_PER_DAY` (default 900) and `TTS_CHARACTERS_PER_DAY` (default 45,000; set from the measurements below), per UTC day, for the whole app. Both are Bicep parameters, so `azd env set MODEL_CALLS_PER_DAY 0` then `azd provision` forces cached mode. 0 trips the guard at once.
- **Counting.** `PunditPipeline` reports every model call *before* making it (live windows, questions and the recap, retries included), so a failed call still counts. TTS characters are counted before each synthesis. Counts are in memory: one replica; they reset at midnight UTC and on a restart.
- **When either cap is reached:**
  - A new viewer's `/start` returns `mode: "cached"`, `audio: false` and a short notice: "The pundit has reached today's limit, so this match uses pre-written commentary with audio off. Live commentary is back tomorrow." The page shows it as a card in the feed and disables the mic. That replay uses the cached pipeline: pre-written lines, cached Q&A, the deterministic recap, no model calls and no speech. Lines arrive with `audio_pending: false`, so no card waits for audio.
  - Start works even if the model warm-up failed, since cached mode doesn't need the model; `/healthz` reports `ready: true` and `limited: true`.
  - Questions on a replay that was already running live are answered from the cache once the model-call cap is reached.
  - Speech stops everywhere once the TTS cap is reached: the text is still delivered, and the audio event says "unavailable", as for any TTS failure.
  - Live windows of replays already running continue until they end. At most 6 replays run, so the overshoot is bounded by those matches (about 34 calls each).
  - Spoken questions on a limited replay get a polite 409 ("Spoken questions are off for this match. Please type your question.").
- **Logging.** `spending_guard_tripped kind=model_calls|tts_characters used=… cap=…` once per cap per day (WARNING, so it reaches App Insights), and `replay_started_limited` for each limited replay.
- **Tests** (`tests/test_spending.py`): counting, a single log per trip, the daily reset; with the cap at 0, a whole m01 in cached mode with no live-pipeline call and no speech, the notice, a typed answer from the cache and the spoken-question refusal; a TTS cap reached mid-replay gives `answer_audio_unavailable` with the text delivered.

### Warm-up

- **Starting container.** While the container starts (a new revision, a restart, or scale-from-zero with `MIN_REPLICAS=0`), Container Apps ingress holds the first request until the startup probe on `/healthz` passes; uvicorn is up in a few seconds. The page and `/healthz` arrive as soon as it is.
- **Model warm-up.** The page then shows "Warming up the pundit… Ns" (a running count in the status chip and in the feed's empty state, which is the part phones show) until `/healthz` reports ready. Start stays disabled until then.
- **Errors during start-up are not errors.** A 5xx or failed request from `/healthz` or `/api/matches` (for example while a revision swaps) keeps the warming message and retries every 2.5 s; nothing is shown in red.
- **Polling stops once ready** (review 2 finding 17), so an open tab doesn't keep a scale-to-zero app awake. A failed warm-up is retried every 15 s.
- The first paint already says "Warming up the pundit…", before any script runs.

### Review 2 container-readiness table

| Row | Status | How |
| :-- | :-- | :-- |
| Container image | Done | `python:3.12-slim-bookworm`, `ca-certificates`, `libssl3`, `libasound2`, non-root user (group 2). |
| Working-directory paths | Done, differently | No `DATA_DIR`: `WORKDIR /app` with `data/cached` copied in. The API generates matches in memory (about 130 ms per Start) and never writes `data/matches`; nothing is written at runtime except temp audio under `/tmp`. A read-only, non-root filesystem works. |
| Audio storage | Done | In memory per replay, cleared when the replay stops; `/tmp` is writable for the synthesis temp file. Bounded by the TTS cap. |
| In-memory state | Done | `maxReplicas: 1`, one uvicorn worker (`serve.py`). README note on restarts: Phase 5. |
| Port and bind address | Done | `0.0.0.0:8000` from `serve.py` (`PORT`), Bicep `targetPort` 8000, no `--env-file`. |
| Health checks | Done | `/healthz` for startup (60 s), liveness and readiness; no model or speech calls (tested); 200 while warming, so the page can say so. |
| Cold start | Done (page); README note in Phase 5 | Elapsed-seconds count on the warm-up chip and feed. `MIN_REPLICAS=1` keeps judging warm. |
| SSE through ingress | Done | 240 s request timeout from Learn; resume with `Last-Event-ID` and a 200 s server close (group 1). |
| Managed identity | Done | `AZURE_CLIENT_ID` set to the user-assigned identity; `DefaultAzureCredential` and the Azure Monitor exporter use it. |
| Logging | Done | Root logging at INFO in `serve.py`, Azure SDK at WARNING; app logs reach App Insights through `configure_azure_monitor`. |
| Cost guardrails | Done | Findings 7 and 12 fixed in Session B group 3 (stream deadline, replay slots, right-most `X-Forwarded-For`); per-replay token and per-address cap (group 3); the daily spending guard. |
| Speech role (added in Session B) | Done | Cognitive Services Speech User on `pundit-speech-swc-kl`, scoped to the resource (group 2). |
| Ingress request timeout (added in Session B) | Done | Group 1. |

## Phase 4, before the gate

### What one live match costs (measured on the deployed app, 17 Oct 2026)

m01 at 20x, `NARRATIVE_MODE=live`, Harry voice, one replay at a time from a script (`Last-Event-ID` resume across the 200 s close in every run). Model calls are the app's `agent_call` logs, which are never sampled; tokens are the Agent Framework `chat` spans; TTS characters are `speech_synthesized ... characters=` (the text sent to `synthesize_mp3`, which is what the guard counts).

| Run | Model calls | Input tokens (cached) | Output tokens | TTS calls | TTS characters |
| :-- | --: | --: | --: | --: | --: |
| No questions | 34 | 52,275 (5,632) | 9,029 | 14 (13 lines, recap) | 1,522 |
| 3 typed questions | 34 (3 explainer) | 53,526 (9,728) | 8,497 | 17 (13 lines, recap, 3 answers) | 1,777 |
| Earlier pair, from logs only | 32 / 36 | — | — | 14 / 18 | — |

Live commentary varies by a few calls from run to run (director and commentator calls depend on which windows the tactical check sends), which is as much as three questions add. Typed answers' text came back in 2.28, 3.44 and 2.77 s from the script (4.36, 6.27 and 3.61 s in the earlier run).

**Caps:** 20 matches a day with questions at the highest observed use (36 calls, 1,800 characters), plus 25%: **900 model calls** and **45,000 TTS characters** a day (UTC). At list prices (below) a capped day costs at most about $2.50 of model use and $0.70–1.00 of speech.

**Spans were being sampled.** The first pair of runs showed 24 `chat` spans for 32 logged calls: `azure-monitor-opentelemetry` defaults to a rate-limited sampler (5 traces a second), which drops spans in bursts. `OTEL_TRACES_SAMPLER=always_on` is now set in Bicep; in the second pair, spans and logs agree exactly (34 = 34 calls, 14 and 17 syntheses with the same characters). The volume is tens of spans per match.

### Replay cap 6, and a friendly "busy"

- `MAX_CONCURRENT_REPLAYS` defaults to **6** (was 3; the build prompt's non-negotiable 7 and the Copilot instructions still say 3 and need updating). The per-address limit stays at 3, so one office can't take every slot.
- When every slot is in use, `/start` returns 503 with `Retry-After: 60` and `{"detail": {"code": "busy", "message": "The pundit is busy, try again in a minute."}}`. The page shows that message as a card in the feed with a **Try again** button (a click, so autoplay stays unlocked), re-enables Start, and shows nothing in red. Checked in a browser against a one-slot local server.
- Tests: the default cap is 6 with 3 per address (two offices of 3; a seventh viewer gets "busy"); the busy body and `Retry-After`.

## Phase 4 gate, 17 Oct 2026 (deployed URL)

`https://ca-pundit-bcd5v7aw3xhmg.salmonbeach-9f471d5c.uksouth.azurecontainerapps.io/`

| Check | Result |
| :-- | :-- |
| `azd up` from a clean terminal | A new `pwsh -NoProfile` process: SUCCESS in 2 min 18 s (provision 1 min 4 s, deploy 1 min 14 s), into `rg-pundit`; still no other `pundit` resource group. The live revision has the new settings (caps 900 / 45,000, `OTEL_TRACES_SAMPLER=always_on`, 6 slots). |
| m01 in full at 20x, Harry, autoplay, across a reconnect | Playwright Chromium, Start clicked. 313 s across 2 connections; the page reconnected after the 200 s close with `Last-Event-ID: 1075`. 1,548 events, seq 1–1548, each once and in order; 13 lines, none repeated, **none before an event it cites**; final score 3-2; 0 `error` messages; the "Enable audio" button never appeared. 13 line clips queued, 11 played with no click after Start; the 2 not played were in a burst of 5 lines in 25 s and expired after 12 s in the audio queue (Session B design: the voice never falls more than one line behind). 3 answer clips and the recap clip played. |
| Typed question: spoken answer, text in under 6 s | Browser, timed from the form's submit event to the answer card: **2.87 s** and **3.28 s**; a first question timed from before the key press took 6.24 s (server turn 5.67 s: the evidence check rejected attempt 1 for an ungrounded "57" and the retry passed). Each answer was then spoken. Script runs: 2.28, 3.44, 2.77, 4.36, 6.27 and 3.61 s. **2 of 9 answers over 6 s**, both on an evidence-check retry. |
| Traces in Application Insights | For the gate run: 35 `chat gpt-5.4-mini`, `invoke_agent` Commentator 14, Analyst 8, Director 7, Explainer 4, Recap 2; `pundit.turn` live 15, question 3, recap 1; `pundit.speech.synthesize` 17; `GET /msi/token`. One question's operation: `pundit.turn` → Explainer → `chat` → TTS. `genAIContent` (50 rows a day) has empty input, output, system-instruction and tool-call fields; only tool schemas are stored, so no prompt or answer text reaches telemetry. |
| Spending cap forced to 0 | `azd env set MODEL_CALLS_PER_DAY 0`, `azd provision`: `/healthz` `ready: true, limited: true`, chip "Pundit ready · cached". Start in the browser: the notice card, "pre-written commentary" in the status line, mic disabled, cached lines with no audio events and nothing played, no errors. Restored with `azd env set MODEL_CALLS_PER_DAY 900` (`azd env unset` didn't remove the value) and `azd provision`: `limited: false`. |
| No keys in the repo, the image or the deployed settings | Repo: a pattern scan (account keys, SAS, private keys, instrumentation keys, API-key and secret assignments, common token prefixes) of every tracked file and of `git log -p --all` found nothing; no `.env`, `.azure/`, `.pem` or `.pfx` was ever committed. Image (an `az acr run` of the deployed tag): `/app` holds `LICENSE`, `README.md`, `build`, `data`, `pyproject.toml`, `src`, `third_party`; no `.env`, `.azure`, `.git`, `.venv`, screenshots or key files (only the public CA bundle); no key patterns under `/app`; the image environment has no secrets; runs as `pundit` (uid 10001). Deployed: `secrets` null, no registry password, no key-like setting values, App Insights local auth disabled. |

**Open:** the existing Speech resource `pundit-speech-swc-kl` still has local (key) auth enabled, and the Foundry account doesn't report the flag. The app never uses keys; disabling local auth on both would close that door.

### Monthly cost estimate (USD list prices, 30 days)

Prices from the Azure Retail Prices API, 17 Oct 2026: Container Apps UK South $0.000034 per vCPU-second active, $0.000004 idle, $0.000004 per GiB-second; free grant 180,000 vCPU-s and 360,000 GiB-s per subscription per month; no environment fee without a Dedicated profile (Learn). ACR Basic $0.1666 a day. Log Analytics $2.88 per GB. `gpt-5.4-mini` Data Zone, Australia East: $0.90 per 1M input, $0.09 cached input, $5.40 output. Speech: Neural TTS $15 and Neural HD $22 per 1M characters; **the MAI voice has no retail price listed, so $15–22 is an assumption**. STT $1 an hour.

| | `minReplicas: 1` | `minReplicas: 0` |
| :-- | --: | --: |
| Container Apps compute | $13.40 idle floor (0.5 vCPU, 1 GiB, all month, after the free grant); about **$19** with 20 matches a day (an open stream counts as active); $47 if a tab is open all month | $0 for light use (inside the free grant); about $4 at 20 matches a day (including the 5-minute scale-in) |
| Container Registry, Basic | $5.00 | $5.00 |
| Log Analytics / App Insights | ≤ $3 (measured 0.66 MB per match, about 3.6 MB a day idle) | ≤ $3 |
| **Fixed** | **about $27** | **about $8** |
| Per match with 3 questions (measured) | model $0.086 + speech $0.027–0.039 = **$0.11–0.13** | same |
| 20 matches a day (600 a month) | +$68–75 → **about $95–100** | +$68–75 → **about $77–83** |
| 5 matches a day | +$17–19 → **about $45** | +$17–19 → **about $26** |
| Ceiling set by the daily caps | usage ≤ $3.30 a day (900 calls ≈ $2.28, 45,000 characters ≤ $0.99) → ≤ $98 a month + fixed | same |

## Polish session, 17 Oct 2026

Wording and small fixes only; the pipeline structure, timing, evidence rules, streaming and infrastructure are unchanged. The wording rules live in `agents/wording.py` and run on every line before the evidence check; they only remove words or swap one accepted team name for another, so a grounded line stays grounded.

### How lines are worded

- **Clock.** A line may give the minute or the time left only with a clock budget: none of the last 3 lines mentioned the clock and clock lines stay at 1 in 4 or fewer (`clock_budget_allows`), and only for goal, red-card, big-chance and formation tiers. The model gets `clock_allowed` and, without it, no clock facts. A leading "At 72 minutes," / "At minute 72," is removed rather than rejected; a line with the time left loses its minute; without a budget every clock reading ("with 18 minutes left", "67 minutes gone") is removed. The dispatcher applies the same rules when it publishes, so the two lanes can't both spend one budget.
- **Formations.** "A back three" only when the first number of the formation changes. The fallbacks add ", a back three now" in that case and nothing otherwise; a model line that names an unchanged back line is retried, then falls back.
- **Team names.** Full name first in a line, short form after, from an explicit table in `engine/teams.py` (Ashbridge, Port Calder, Merehaven). `team_display_name` and the score phrases now carry the full name, so a line that opens with the score says "Port Calder FC lead 1-0". Each recap field follows the same rule. The evidence check takes both forms from the table instead of stripping "FC".
- **Banned phrases** (model lines only; a hit goes straight to the deterministic line): hype words, betting words, invented detail (the crowd, the home supporters, atmosphere, this afternoon, tonight, VAR, injury, injured), "the winner", and the openers And, What a, There it is, Here we go, The home side, The visitors. Whole-word and case-insensitive. "Comeback" and "turnaround" are allowed only for a team that trailed earlier and is now level or ahead. "The referee", "It's" and "It is" are not banned.
- **Cached content.** The cached lines, recaps and answers were reworded to the same standard (no nickname openers, no "home supporters" or "afternoon", nicknames replaced in recaps and answers, full name then short), and a test runs every cached line through the banned-phrase and evidence checks.

### Numbers and duplicates

- **Possession pairs sum to 100.** `possession_pair` rounds home half up and gives away the rest, for the one-decimal value and the whole-number spoken value, in `match_state`, `possession_window`, `match_snapshot` and `compare_windows`. "61% to 40%" now fails the evidence check. The stats panel has no possession figure; that comes in a later UI session.
- **One line per red card.** The card reaches the fast path and the tactical lane about 0.3 s apart (review 2 finding 14); the dispatcher publishes the first line citing the card's event ID and skips the other.

### Audio and the interface

- **Audio queue.** Goal, equaliser, late go-ahead, red-card and big-chance clips are never dropped and never go stale; in a burst the oldest routine clip goes first. Tested by running the shipped `AudioQueue` class under Node.
- **"Checking the facts…"** When an answer fails the evidence check and is retried, the API sends `answer_checking` on the replay's stream. Typed questions now send a page-chosen `question_id` (validated) so the event can find the waiting card.
- The line label for missing audio is now "Audio off".

### Diagnostics

- Every failed check is logged as `evidence_check_failed … rule=… subject=…` (the number, name or ID and the rule broken) and kept on the window result. `--live-latency` prints a summary by reason, token totals, and the gate's wording checks.
- **Token counts.** The workflow's `AgentResponse` arrives with `usage_details` None; each `ChatResponse` has the counts. A chat middleware (`UsageCapture`) now adds them up per agent call, so `agent_call` logs, `turn_tokens` logs and the `pundit.turn` totals carry real numbers.

### Gate, 17 Oct 2026 (local, live models, 20x)

| Check | m01 | m02 |
| :-- | :-- | :-- |
| Lines (agent) | 13 (9) | 11 (9) |
| A minute and "minutes left" in one line | 0 | 0 |
| Clock lines | 0 of 13 | 0 of 11 |
| "FC" after a team's first mention | 0 | 0 |
| Red-card lines | — | 1 |
| Banned-phrase rejections | 0 of 15 commentator calls | 0 of 13 |
| Evidence-check failures by reason | 13: event not supplied to the agent 5, ungrounded number 3 ("10" twice, "18"), away leader's score 2, score mismatch 2, xG decimal 1 | 7: event not supplied 2, away leader's score 2, score mismatch 2, score with no score_phrase 1 |
| Tokens (input / output) | 46,347 / 5,849, every call counted | 40,869 / 5,262, every call counted |

- **Browser** (Playwright Chromium, m02 then m01 at 20x, live): every queued clip played. m02: 11 line clips (goal, red card, equaliser, late go-ahead), the answer and the recap. m01: 13 line clips (all five goals) and the recap. With the first answer's evidence check forced to fail by a gate-only launcher, the answer card showed "Checking the facts…" and the answer then replaced it: "Port Calder FC have 60% possession and Merehaven Athletic 40%", with facts of 59.7% and 40.3%.
- No line in either latency run spoke a possession pair; the browser answer above is the pair seen live. The pairs are covered by tests for every window and snapshot in m01–m03.
- The browser m01 run had one clock line, "… 67 minutes gone." on the 67' formation line (within the budget). "Minutes gone/played" wasn't recognised as a clock reading; it is now.

## Polish follow-up: wording accuracy, 17 Oct 2026

Deterministic post-checks and template changes; the evidence check is unchanged. A model line that fails one of the wording checks goes straight to its deterministic line (no retry), like a banned phrase.

- **Scoring-team phrases.** On a goal line, "back on terms", "level it", "the equaliser", "the opener", "struck first", "a goal back" and similar must follow the scoring team: the nearest team named before the phrase in its sentence must be the scorer (`scorer_phrase_failures`).
- **No "consolation" live.** Out of the commentator instructions, on the live banned list, and the goal-context fact is shown to the commentator as "a goal back". The recap keeps "a goal back".
- **Possessives.** A name ending in s takes an apostrophe only ("Ashbridge Rovers'"), on every line.
- **Phase words fit the minute.** "early" before 15'; "before the break" and "late in the half" from 35' in the first half (stoppage time included); "early in the second half" up to 55'; "late on" and "late in the game" from 80'. A misplaced phrase is removed; if that leaves a broken sentence (too short, a dangling word, "an" before a consonant), the line falls back.
- **Final punctuation** on every line, cached ones included.
- **Fallback variety.** Every tactical fallback kind has 3 to 6 versions; the dispatcher keeps the templates used in the match and a fallback picks an unused one first.
- **No empty restating.** Lines other than goals and red cards never get the score phrase at 0-0; the existing rule (not when any of the last 3 lines gave it) covers the rest.
- **Comparisons.** "up from N%" must name the period it compares with; "N-point swing" is banned. The possession fallback says "up from 45% in the 10 minutes before that".
- **Fallback wording.** "the better of the expected goals" became "the better of the chances" (without the window number, which the check can only ground next to "xG").
- **Late drama.** A peak goal from 80' may give its minute even soon after another clock line; the 1-in-4 cap still holds.
- **Docs.** The build prompt's replay lines now say 6 slots, 3 per address, 429 and 503, and the resumable stream.

### Live gate, m01 and m02 at 20x

Every new check is clean in both runs: no line without final punctuation, no "Rovers's", no "consolation", no point swing, no tactical 0-0, no repeated fallback line, no wording rejection, no clock line, no "FC" after a first mention, one red-card line in m02, tokens counted for every call.

The model share fell: m01 8 of 13 lines from the model (9 before), m02 5 of 11 (9 before). The fallbacks came from evidence failures on both attempts, led by:
- an ungrounded "10" (9 times across the runs; "ten" once): the model says "the last 10 minutes" without a citable window fact in that sentence;
- "score stated when score_phrase was null" (4 in m02): with the new 0-0 rule the tactical lines get no score phrase, and the model still writes "The score is level at 0-0".

### Follow-up 2: recovering the model's lines, 18 Oct 2026

- **Window lengths are grounded.** "the last / previous / past N minutes" passes the evidence check when N is a window length configured in `engine/analytics.yaml` (today 10 and 5), in digits or words. Any other N, or N in another phrase ("for 10 minutes"), still needs a fact.
- **A withheld score is removed, not rejected.** With no score phrase, a short trailing score sentence ("The score is level at 0-0.") is cut; a score anywhere else still fails the line check.
- **Comparison example** in the commentator instructions is now "up from 45% before that" (no window number).
- **Possession fallback:** "{team} have taken over the ball: {after}% of the possession in the last {window in words} minutes, up from {before}% before that." "of the possession" is there because the check grounds a possession percentage only in a sentence that says possession. The "N points more of the possession" version is gone.
- **"a man short"** (and "down", "light") becomes "a player short" in the same pass as the team names (`apply_house_style`), cached lines included.

Live, 20x: m01 11 of 13 lines from the model (target 9), m02 8 of 11 (target 8). All wording checks clean; one clock line in m01 (8%). Remaining fallbacks: two m02 goal lines whose replies stated the score without citing the score fact ("score does not match" 5, away-leader rule 2), and three tactical windows.

### Follow-up 3: score citation, match averages, red cards, 18 Oct 2026

- **Score citation.** When a model line contains the supplied score phrase (in full or with the short form the house style gives it), the server adds the score fact to the line's citations. A wrong score still fails.
- **No raw match averages for the commentator.** The high-turnover signal carries a rate description fact ("well above their usual rate" at 2x the match average or more, "above their usual rate" at 1.5x, otherwise none). A cited average stays in the Insight, so the Insight's own check holds, but the commentator's facts and fact IDs leave it out, the description comes in, and an average cited back in a line is dropped, so its number can't be grounded.
- **Red cards.** The instructions ask for the consequence ("a player short"; "down to ten men" only with the players-on-the-pitch fact) rather than repeating that a card was shown.

Live, 20x: m01 10 of 13 lines from the model, m02 8 of 11; both m02 goal lines are model lines now. No raw average was spoken; the m02 red-card line gave the consequence. The scorer-phrase check sent one m01 line (81', the equaliser) to its fallback.

This ends the commentary changes for the polish session.

## Mobile session, 18 Oct 2026

### Group 1: phone layout (under 1024px)

**The overflow.** `.app-shell` is a grid with no `grid-template-columns`, so its one column was the implicit `auto` track, which grows to its widest child's min-content width. A `nowrap` row (the footer's "All fixtures and players are fictional." beside the connection text, the ask row's Talk and Ask, the control buttons) set that width. At 390px with 130% text the shell was 431px wide. `html, body { overflow: hidden }` hid this from `scrollWidth` (390) in Chromium; iOS Safari doesn't clip it the same way, which is why the Resume button, card text, the send button and the footer were cut off on the right. Fixes:
- `.app-shell { grid-template-columns: minmax(0, 1fr) }`: no child can widen the page.
- `body { min-width: 320px }` removed: Safari's 130% page zoom on a 360px phone is a 277px CSS viewport.
- `min-width: 0` on the control bar's and the ask row's flex children; card tops and card actions wrap; `.feed-card { overflow-wrap: anywhere }` breaks long words.

**Header.** Under 1024px it is one row: short team names (from `engine/teams.py`, now on `/api/matches` as `home_short` / `away_short`), the score, the clock and a connection dot. The brand badge is hidden. The live light is now the connection dot: grey before a match (hidden on phones until there is a connection; the warm-up dot covers that time), green when connected, pulsing gold while reconnecting, red when lost. Its label and title carry the connection text.

**Controls.** Two groups: setup (picker, speed, Start) and run (Pause/Resume, Mute, Crowd, Match info). On the desktop layout they are one row, as before. Under 1024px the page's `data-phase` picks what shows: `idle` shows picker and speed on one row, then Start and Match info; `running` shows only the icon row (labels kept for screen readers); `done` (after full time) shows both, so "Start another match" sits beside Mute and Match info. Crowd is hidden on phones until it's built.

**Pause/Resume is one toggle on every screen size** (`#pause-button`, `data-paused` switches icon and label). Mute is a button with `aria-pressed` instead of a checkbox, on every size.

**Ask bar.** The box shrinks (`min-width: 0`); Talk and Ask are icons on phones, with text labels kept for screen readers and shown on the desktop; the buttons don't shrink, so the send button is always whole. An empty box is one line with an ellipsis, and the phone placeholder is "Ask about this match…".

**Footer.** "All fixtures and players are fictional." moved to the foot of Match info (the left column on the desktop, the drawer on phones), with the connection text beside it on phones. The footer row under the ask bar is dropped on phones; on the desktop it keeps only the connection text.

**Possession.** The stats panel (and so the drawer) has a Possession row. The server adds `possession` to every released event: each side's share of successful passes among released events (the analytics' measure), as whole numbers from `possession_pair`, so the two always add up to 100. It is `null` before the first successful pass, shown as "–".

**Checked** in Playwright Chromium (mobile emulation, 3x) against the cached-mode server, before Start and while running with a long unbreakable word added to a card and a long question typed. 130% text was checked two ways: every stylesheet font size times 1.3 (Android's text scaling) and 130% page zoom (Safari's aA control, which narrows the CSS viewport to width / 1.3). `scrollWidth` was also read with the overflow clip lifted from `html`, `body` and the shell, so hidden overflow couldn't pass.

| Width | Text | `scrollWidth` (clip lifted) vs `innerWidth` | Header + controls, before Start / running |
| :-- | :-- | :-- | :-- |
| 360, 375, 390, 430 | 100% | equal at every width | 129 / 89 px |
| 360, 375, 390, 430 | fonts 130% | equal at every width | 136 / 96 px |
| 360, 375, 390, 430 | page zoom 130% (277–331px) | equal at every width | 129 / 89 px |

At 390×844 that is 15.3% before Start and 10.6% running (limit 20%). No element's right edge passed the screen in any case. 1366×768 and 1920×1080 keep the two-column layout with no overflow.

**Possession definition (checked after group 1).** The panel's figure and every possession fact the commentary and Q&A use come from the same definition: each side's share of successful passes, through `possession_pair`. `match_snapshot`'s whole-match fact is over the events up to `as_of`, the same prefix as the released events the panel counts. `tests/test_mobile_layout.py` compares the panel's pair with `_possession_split` along all three matches and with the snapshot fact the Explainer cites.

### Group 2: leaving and coming back (lock screen, app switch, tab switch)

On a real iPhone, locking the screen stopped the match and it never came back. The stream died with the page; after the 30 s reconnect grace the replay was stopped, and the page's reader gave up silently on the 404.

**Server (changes Phase 4 group 1's "a drop doesn't stop the replay").**
- **A dropped stream pauses a running replay at once** (`replay_paused_on_disconnect`), match clock and broadcast delay together, through the same `set_paused` as `/control`. A locked phone no longer misses the match.
- **The planned close is not a drop.** The stream's own close every `STREAM_MAX_SECONDS` (inside the ingress timeout) leaves the replay running, with the 30 s `STREAM_RECONNECT_GRACE_SECONDS`, because the page reconnects a second later.
- **A paused (or finished) replay with no stream is kept for `STREAM_PAUSED_GRACE_SECONDS`** (default 600, configurable) before it is stopped and freed. A paused replay releases no events, so no window starts and no model or speech call is made. A call already in flight when the pause lands finishes; a window whose line is written waits for its event's release and calls nothing more.

**Page.**
- **Hidden** (`visibilitychange`, and `pagehide` for Safari's back-forward cache): if the match is running and the viewer hadn't paused it, the page pauses its audio queue and sends `/control` pause with `keepalive: true`, so the request finishes while the page is frozen. If it doesn't arrive, the server pauses when the stream drops. The page then closes its own stream rather than leave a frozen socket.
- **Shown again:** the page reopens the stream with `Last-Event-ID`, and if it paused the match it shows a "Welcome back. The match paused while you were away." card with **Tap to resume**. The tap resumes audio first, inside the gesture (the clip that was playing, the next one, or a silent unlock if nothing is queued), as iOS requires after a lock, then resumes the match. The Pause/Resume toggle does the same.
- **An unplanned drop while visible** (a signal blip, the ingress cut) also pauses the match on the server. After any reconnect the page sends a quiet resume unless the viewer paused or the Welcome back card is waiting, so a blip costs about a second, not a tap.
- **Gone:** a 404 from the stream or from `/control` (the replay was freed after 10 minutes) shows "This replay has ended." with **Start again**, sets the connection dot red, and disables the controls. It never stops silently.
- **Audio staleness leaves out time paused.** A routine clip is dropped if it hasn't started 12 s after its line arrived; time spent paused no longer counts, so lines waiting when the phone was locked still play.

**Tests** (`tests/test_away.py`, in-process, cached mode): a drop pauses at once and the match clock doesn't move; the planned close doesn't pause; a paused replay outlives the running grace and is freed after the paused grace, then the stream gets 404; coming back within the grace resumes with contiguous message IDs and each line once; no pipeline or speech calls while paused (counted over 2 s, about 40 match minutes at the test clock), and calls start again on resume. `tests/test_stream_resume.py`'s mid-match and new-address tests now resume after reconnecting, as the page does. Mutation checks: with the disconnect pause removed, 4 of the 5 away tests fail; with the planned close treated as a drop, the planned-close test fails.

**Checked in a browser** (Playwright Chromium, 390×844 phone emulation, m01 at 20x, cached mode with real Azure Speech). The lock was simulated by firing `visibilitychange` with `visibilityState` hidden, taking the browser offline for the lock, then online and visible again. Two viewers ran side by side.
- **2-minute lock.** Hidden at 23' with a line clip played. The keepalive pause reached the server. On return the Welcome back card showed, and for 5 s before the tap nothing moved: no events, no clock. The clock read 24' rather than 23', from events already sent between the page closing its stream and the pause landing (at 20x one match minute is about 3 s). After the tap the match resumed: 448 messages handled with last ID 448 (no gap, no duplicate), event seqs contiguous and unique, each line once, and a line clip played after the tap (`play()` resolved, `playing` fired). No page errors.
- **11-minute lock.** The server freed the replay at 600 s (`replay_stream_not_reconnected … seconds=600`). On return the page showed "This replay has ended." with Start again, the dot red. Start again ran a new replay.

Not covered here: a real iPhone. In particular, whether iOS delivers the keepalive request before freezing the page, and whether Safari fires `visibilitychange` on lock. The server pause on the dropped stream covers the first case.

### Group 3: slot eviction and test reliability

**Slot eviction.** When a Start needs a slot and all `MAX_CONCURRENT_REPLAYS` are taken, the server stops the replay whose viewer has been gone longest: paused, no stream connected, ordered by when its stream dropped (`away_since`). It is logged as `replay_evicted`. Its viewer gets a 404 on return and the page shows "This replay has ended" with Start again. A replay with a viewer connected is never evicted, so with everyone watching a full app still answers "busy". The per-address cap (3 replays per address) does the same before refusing with 429: that address's longest-gone paused replay gives way. The gate found this case: with the 10-minute hold, a household where one phone is locked would otherwise have kept a third viewer on the same network out. Tests in `tests/test_away.py`: six slots full with one viewer gone, a new Start succeeds and the gone viewer's stream gets 404; the viewer gone longest goes first; with everyone watching the app is still busy; at the address cap a gone viewer gives way and three present viewers still cap it. With eviction switched off, the first two fail. In a browser (two slots): A started and locked, B started, C's Start took A's slot, and A came back to "This replay has ended" while B kept running.

**The event-loop tests.** One run's longest event-loop gap is a single sample. In the full suite it sometimes caught a pause of the whole test process: 398 ms in the 7-minute test (limit 100 ms) and 266 ms in the full-match test (limit 200 ms), both well under 100 ms alone. A probe with a 3-million-object heap didn't reproduce it and saw no full collection, so garbage collection isn't proven as the cause. The fix doesn't depend on the cause:
- **Median of several runs.** Each test now measures 5 runs (the 7-minute test, about 0.7 s each) or 3 (the full match, about 13 s each) and asserts on the median of the runs' longest gaps. A blocking call in the dispatcher blocks every run, so the median catches it; a one-off pause lands in one run and doesn't move the median. The thresholds stay 100 ms and 200 ms in and out of the suite. A higher threshold just for the suite was rejected because it would let a real block of up to that size through in the run that matters.
- Garbage is collected before each run, and any full collection during a run is printed with its length, so a slow run in the suite shows whether GC was involved.
- **Still catches a real block.** With the tactical evaluation moved back onto the event loop (no `to_thread`), every run showed it: 250, 251, 251, 250, 251 ms (median 251, limit 100) and 509, 541, 501 ms (median 509, limit 200). Both tests failed.

**Fixed short waits.** Every wait in the suite was reviewed. A wait for something that should happen now waits for that condition, with a generous upper limit that only stops a hang:
- `test_stopping_replay_cancels_in_flight_agent_runs`: the pipeline call starting had 1 s; now 10 s.
- `test_replay_pause_resume_preserves_order_and_as_of`: delivery 1 s → 10 s.
- `test_api` warm-up: a 0.15 s sleep before "ready" → poll `/healthz` until ready, up to 10 s.
- `test_trust_and_accuracy` broadcast pause: a 0.02 s sleep assumed every event was stamped and none released, with a 0.1 s delay, so any stall over 0.1 s released events early. Now it waits until all are stamped, then pauses; the delay is 0.5 s and the hold 1 s. The completion limits went from 1–2 s to 10 s.
- `test_stream_resume` paused reconnect: a 0.4 s sleep to "let the outbox drain" → wait until the outbox stops growing (`_settled`, shared with `test_away.py`). The stale-connection wait went from 1 s to 10 s.
- `test_away` paused grace: the "still there" check now has a 3 s grace against a 1 s look, not 1.5 s against 0.8 s.

Left as they are: waits that check something *doesn't* happen (load only makes those more lenient), waits that are part of the scenario (time passing while away), `sleep(0)` yields, and polling loops that already had deadlines.

**A test that passed for the wrong reason.** `test_sse_disconnect_stops_the_replay_after_the_reconnect_grace` asserted after `asyncio.run` returned, when app shutdown had already stopped every session. After group 2 the grace never stopped that replay (the drop pauses it for 10 minutes), and the test still passed. It is now `test_sse_disconnect_pauses_then_frees_the_replay_after_the_grace`: it checks inside the app's lifetime that the drop paused the replay and that it was freed after the paused grace.

### Gate, 18 Oct 2026 (local)

| Check | Result |
| :-- | :-- |
| Layout at 375, 390, 430 (Playwright Chromium, phone emulation; normal text, fonts 130%, page zoom 130%) | `scrollWidth` equal to `innerWidth` in all 18 cases, also with the overflow clip lifted; no element past the right edge. Header plus controls at 390×844: 129 px (15.3%) before Start, 89 px (10.6%) running |
| Simulated 2-minute lock (m01, 20x, real Azure Speech) | Welcome back card; nothing moved before the tap; 447 messages handled, last ID 447; event seqs contiguous and unique; each line once; a line clip played after the tap |
| Simulated 11-minute lock | Freed at 600 s (`replay_stream_not_reconnected … seconds=600`); "This replay has ended" with Start again, which started a new replay |
| Slot eviction (browser, 2 slots) | The locked viewer's slot went to the new Start; that viewer came back to "This replay has ended" |
| Full suite twice in a row | See the session report |

Screenshots: `docs/screenshots/mobile/` (1×). iPhone checks of groups 1 and 2 (layout, 1-minute lock, app switch, airplane-mode blip) passed on the deployed app.

### Group 4: deterministic colour lines

The replay may release a colour line after 12 real seconds with no speech playing or queued, and at least 40 real seconds after its previous colour line. The viewer reports queue changes to the server. Colour lines are spread by match clock: none before 10', no more than one in any rolling 15-minute span, and none after 85'. The replay pauses its colour worker while paused or disconnected. A queued candidate is invalidated when a real event arrives; a must-say event still inside its broadcast delay also blocks publication.

Possession, shots on target, turnovers with the existing “usual rate” description, and formation matchups each have up to three deterministic template versions. Versions and their underlying statistic windows or signatures are each used at most once, leaving valid second-half templates available. Every candidate passes the existing wording, team-name, banned-phrase, evidence, and plain-spoken checks, then uses the routine audio tier and `source="colour"`. Missing or unsupported facts simply make that version unavailable.

## Review 3 fixes (18 Oct 2026)

`docs/reviews/review-3-opus.md` has the findings. One change to group 4 above: the colour candidate is now built in a worker thread (each build is 0.6–2.4 s of CPU, which had blocked every viewer's stream). Events keep arriving while it builds, so a routine event no longer invalidates it; a real line or a change in the viewer's audio queue still does, and a candidate is dropped if a shot or a must-say event reached the viewer while it was being built. Attempts are at least 2 s apart. `tests/test_robustness.py` drives the real worker with a slowed build: a median loop gap of 26–39 ms, and 688 ms with the build put back on the loop.

## Personalisation, group 1: the viewer profile (18 Oct 2026)

The official rules' fifth pipeline stage is Personalize (§4.1(i)): the same intelligence should reach an analyst and a casual fan as two different experiences.

- **Profile.** Mode (`casual`, the default, or `analyst`) and My team (`home`, `away` or `neutral`, the default), in `profile.py`. No personal data: two fixed choices.
- **Page.** Two selects. On the desktop they sit in the control bar for the whole match. Under 1024px they share the second row with Start before a match and after full time; while a match runs they are in Match info ("Your view"), since the control bar shows only icons then. Both copies stay in sync. The choice is kept in `sessionStorage` (`pundit-profile`) and sent with Start as `mode` and `team`. Team options read "My team: Ashbridge" from 1440px and "♥ Ashbridge" below that, so the control bar stays one row at 1280px and two rows on phones.
- **Server.** The replay holds the profile (`ReplaySession.profile`). `POST /api/replays/{id}/profile` (replay token required, 422 for an unknown value) changes it mid-match. The dispatcher reads it as each window runs, so a change applies from the next line; questions and the recap read it when they run. A reconnect doesn't touch it.
- **Traces.** Every `pundit.turn` span (live, question and recap) carries `app.profile.mode` and `app.profile.team`.
- **Layout checked** in Chromium with the cached server, before Start: header plus controls 129 px at 360, 375, 390 and 430px (15.3% of 844, unchanged from before), and 136 px with fonts at 130% (unchanged), with no sideways overflow. At 130% text on 360–390px the longest team option ("♥ Port Calder") is 12–29 px wider than its select, so it is clipped when chosen. Desktop: one control row at 1280, 1366 and 1920px; two rows at 1024px.
- Mode and My team change nothing the viewer sees yet; group 2 adds the differences.

## Personalisation, group 2: the same facts, a different experience (18 Oct 2026)

Every viewer-facing difference below runs through the same evidence and wording checks as before. A caller with no viewer profile (the latency gate without `--mode`, the console, tests) gets the full-detail writing it always had (`NO_VIEWER_PROFILE`: analyst, neutral). A viewer's replay always carries its own profile, Casual by default.

### Commentary

- **Mode reaches the Commentator** as a `mode` field in its prompt, with a MODE section in `COMMENTATOR_INSTRUCTIONS` (the instructions stay one cacheable string). Casual: the story, at most one number in the line, and the score phrase is that one. Analyst: the numbers within the existing rules, led by chance quality and shape.
- **Casual is checked, not just asked.** `count_numbers` counts a score or a formation once, then every other digit or number word the evidence check reads. A casual model line with more than one is retried with the count as feedback (`casual line with more than one number`), then falls back. A casual line never gets a clock budget.
- **Fallbacks.** `CASUAL_FALLBACK_VARIANTS` says shape, counts and shares in words ("a change of shape", "a player short", "more of the ball"); the one template with a share drops the score. The analyst set is the existing `FALLBACK_VARIANTS`. A test runs a whole m01 with every model line rejected, in both modes: the same beats, every line passing the evidence check, casual lines at most one number, analyst lines up to three and longer on average.
- **Colour lines.** Casual possession and shots lines name only the leading team, with its one number ("Port Calder FC have had 57% of the possession so far."). The formation matchup (two formations) is analyst-only.

### Q&A

- The Explainer prompt gets `viewer: {mode, my_team}`. Casual: one or two short sentences, under 45 words, at most two numbers, no xG. Analyst: up to four sentences. With My team set, the answer says "your side" or "your team" at least once; without one, it never does. Each rule is checked and retried with feedback, like an evidence failure.
- **Framing doesn't change the evidence.** The evidence check reads "your side" (and "your side's") as My team's name (`unframe`), so the number in "Your side have had 57% of the possession" is grounded exactly as in "Port Calder have had 57%…", and the answer shown keeps the framing. A test asks the same question neutral and as an away fan: the framing differs; the facts and events cited are the same.
- **Words must follow the facts.** A live probe found casual answers swapping numbers for comparisons that were wrong ("your side have had more of the ball" for a side on 43%). The evidence check reads numbers, not comparisons in words. The Explainer now has a rule: more of the ball, more shots or the better of the chances only for the team whose fact is higher, and say when it is only the recent spell. In 6 casual probes after the rule, every comparison matched the facts. It is a prompt rule, not a check.

### Recap, interface, crowd

- **Recap.** The prompt gets `my_team`. With one set, the summary's first sentence must name that team (`recap does not lead with My team`, retried). The headline is still winner-first with the score phrase. The deterministic fallback recap stays neutral.
- **Interface.** Casual: Why? opens with the facts and events collapsed under "The facts and events behind it", and the stats table's numbers sit behind "Tap to show the numbers" (switching back to Casual hides them again). Analyst: both open. My team: a ♥ before that team's name on the scoreboard, a 3 px left border in the team colour on its commentary cards, and a faint highlight on its key moments. The server sends `line_team` with each line: a must-say event's team, or the one team behind a tactical window's signals; none for colour lines. Cached mode keeps its neutral content and gets the same interface.
- **Crowd.** A goal swells 1.4× for My team and 0.6× for the other side (neutral: unchanged).
- **Team label.** When a team name doesn't fit its select (360–390px with 130% text), the options read "♥ Home" and "♥ Away". Measured in Chromium: every label fits at 360–430px at 100% and 130% text, with no overflow, and the header is unchanged (129 / 136 px).

### Live gate: m01 at 20x

`python -m second_screen_pundit.agents.gate --live-latency --match m01 --speed 20 --mode casual --team home`, then `--mode analyst --team away`.

| | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| Model-written lines | **10 of 13** | **11 of 13** |
| Numbers per line | 0–1 | 0–3 |
| Evidence failures by reason | event not supplied 6, casual line with more than one number 3, ungrounded number 1, fact not in the Insight 1 | event not supplied 6 |

| Minute | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| 21' | Ashbridge Rovers have more of the ball now, with longer spells under pressure against Port Calder FC. | Ashbridge Rovers have taken control, up to 53% of the possession from 31% before against Port Calder FC. |
| 23' | Port Calder FC have taken the opener, and Ashbridge Rovers must respond. Port Calder lead 1-0. | Port Calder FC have taken the opener from a big chance, and Ashbridge Rovers must respond. Port Calder lead 1-0. |
| 33' | Port Calder FC are having the better of the chances, and Ashbridge Rovers are under pressure. | Port Calder FC have had the better of the chances over the last 10 minutes, with Ashbridge Rovers being pressed back. |
| 57' | Ashbridge Rovers are winning the turnover battle, with 4 high turnovers and a sharp lift in their press. Port Calder FC look under the cosh. | A big chance and four high turnovers in the last 10 minutes, with Ashbridge Rovers well above their usual rate. |
| 61' | Port Calder FC have stretched their lead from a half-chance. Port Calder lead 2-0. | A half-chance and Port Calder FC stretch their lead, with Ashbridge Rovers still under pressure. Port Calder lead 2-0. |
| 66' | Ashbridge Rovers change things up with Loran Mallowfen on for Halen Ironsage, a fresh pair of legs as they look to turn this. | Ashbridge Rovers make a change, with Loran Mallowfen on for Halen Ironsage. |
| 67' | Ashbridge Rovers have changed shape, with the midfield tightened up as they chase this. | Ashbridge Rovers shift from a 4-3-3 to a 4-2-3-1, with the midfield tightened up and 23 minutes left. |
| 72' tactical | *Fallback:* Ashbridge Rovers are forcing mistakes high up the pitch. | *Fallback:* Ashbridge Rovers are pressing and it is working, with 5 high turnovers recently. |
| 72' fast path | *Fallback:* Ashbridge Rovers have changed their shape. | Ashbridge Rovers move to a 3-4-3, a back three now as they chase this after the earlier 4-2-3-1. |
| 74' | Ashbridge Rovers have a goal back, and it's a big lift after that pressure. Port Calder FC lead 2-1. | A big chance, and Kiva Larkspur has given Ashbridge Rovers a goal back. Port Calder FC lead 2-1. |
| 81' | Ivo Juniperby has brought Ashbridge Rovers back on terms from a half-chance. The score is level at 2-2. | A half-chance, and Ivo Juniperby has brought Ashbridge Rovers level. The score is level at 2-2. |
| 84' | *Fallback:* Ashbridge Rovers are carving out the clearer openings now. | *Fallback:* Ashbridge Rovers are carving out the clearer openings now. |
| 88' | Juno Kestrelane has put Ashbridge Rovers in front from a big chance. Ashbridge lead 3-2. | A big chance, and Juno Kestrelane has put Ashbridge Rovers in front again. Ashbridge lead 3-2. |

Two of the casual line-count failures were on the 72' back-three switch: the model kept the formation numbers, so that line fell back to "changed their shape".

**One question each, at the viewer's 60th minute** ("Who has had the better of the match so far, and why?"):
- Casual + Home: agent-written on the first attempt: "Port Calder FC have had the better of it so far, because they lead 1-0 and have more possession. Your side, Ashbridge Rovers, have had plenty of the ball and nearly matched them for shots and xG, so it has been fairly even."
- Analyst + Away: fell back to verified facts after two attempts. The first answer wrote the shot counts as "20-19", which the check reads as a wrong score; the second used numbers in sentences beginning "They", so no team was named. The Explainer rules were then tightened (each number's team named in its sentence, no "they", each team's numbers in their own sentence), and the casual answer checks added. A live probe of the same question afterwards: Analyst + Away, 4 of 4 model answers on the first attempt, all framed ("Port Calder FC lead 1-0, so your side has had the better of it overall. Port Calder FC have 57% possession, 20 shots and 2.044 xG…"). Casual + Home, 6 of 6 after the comparison rule (one needed a retry for three numbers), for example "Your side have had less of the ball, and Port Calder FC have just shaded the shot count too. Port Calder FC lead 1-0, so they have had the better of it so far."

### Comparisons in words are checked (18 Oct 2026)

`agents/comparisons.py`. The evidence check grounds numbers; this checks the comparisons said in words, which casual mode asks for.

- **Phrases**: more or less of the ball or possession, the edge in possession, more or fewer shots or attempts, the better of the chances or clearer openings (each against its own measure); on top, dominating, dominant (true if the team leads on any of possession, shots or xG); under pressure, under the cosh, pinned back, pressed back, on the back foot (true if the other side leads on any of them).
- **Which team**: the nearest team named before the phrase in its sentence, as the scorer-phrase check does; "your side" and "your team" are My team. A "not", "never", "no longer" or "n't" between the team and the phrase reverses it. A phrase with no team before it isn't checked.
- **Against what**: possession share (successful passes), shots and xG, computed from the events up to the line's or answer's position (not from the cited facts). A comparison passes if it is true for the match so far or the current spell, and fails only if it is false for both. The current spell is the last 10 match minutes (the tactical signals' window) or the last 15 (the Q&A snapshot's), so a claim from either set of facts is judged on the same footing.
- **Where**: every commentary line through `_check_line` (model lines, fallback variants and colour lines), Q&A answers (with My team for "your side") and every recap field. A model line or an answer is retried once with feedback naming the phrase, then falls back; a fallback variant that fails is skipped for the next; a recap retries, then falls back. Each case is logged as `comparison_failed route=… text=…`.
- **Tests** (`tests/test_comparisons.py`), at m01's 59th minute: "your side have had more of the ball" for Ashbridge (43%) fails; "Port Calder FC have had more of the ball" passes; "Ashbridge Rovers have had the better of the chances" passes (Ashbridge's xG is lower for the match, higher over the last 15 minutes); a false comparison in a line, an answer and a recap is retried with the phrase named, and the line and the answer fall back after a second failure.

**Gate rerun, m01 at 20x**

| | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| Model-written lines | **11 of 13** | **11 of 13** |
| `comparison_failed` | 0 | 0 |
| Evidence failures by reason | event not supplied 5, casual line with more than one number 1 | event not supplied 6 |
| The 60' question | agent, first attempt: "Port Calder FC have had the better of it so far: they lead 1-0 and have had the slight edge in possession and shots. Your side, Ashbridge Rovers, have had plenty of volume, but not the advantage." | agent, second attempt: "Port Calder FC lead 1-0, and your team have had the slight edge overall: Port Calder FC have 57% possession, 20 shots and 2.044 xG. Ashbridge Rovers have 43% possession, 19 shots and 2.037 xG…" |

The lines' comparisons ("Ashbridge Rovers have the edge in possession now, up to 53%", "Port Calder FC have had the better of the chances over this spell, with Ashbridge Rovers under pressure") all held. The analyst answer's first attempt failed before the evidence step (the reply couldn't be parsed or broke a reply limit such as the 80-word cap); that path isn't logged.
