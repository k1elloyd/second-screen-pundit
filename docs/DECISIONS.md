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
- `WorkflowBuilder`/`AgentExecutor` composes the conditional Director → Analyst → Commentator live route and separate Explainer and Recap runs. Function middleware captures the serialized analytics-tool result after invocation; the evidence checker validates only against those captured facts and the run's fixed `as_of` event sequence. Each two-attempt workflow has one enclosing deadline so retries share the configured budget.
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

## Microsoft Learn sources

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
