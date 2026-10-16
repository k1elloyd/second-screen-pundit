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

## Microsoft Learn sources

- [Microsoft Foundry model provider for Python](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
- [Use the Azure OpenAI Responses API: encrypted reasoning items](https://learn.microsoft.com/azure/foundry/openai/how-to/responses#encrypted-reasoning-items)
- [Microsoft Entra authentication with the Speech SDK](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-configure-azure-ad-auth?pivots=programming-language-python)
- [SpeechConfig Python API reference](https://learn.microsoft.com/python/api/azure-cognitiveservices-speech/azure.cognitiveservices.speech.speechconfig?view=azure-python)
- [Azure Developer CLI schema reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Remote builds with Azure Container Registry](https://learn.microsoft.com/azure/developer/azure-developer-cli/remote-builds)
- [Azure Developer CLI templates overview](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-templates)
- [Bicep file structure and syntax](https://learn.microsoft.com/azure/azure-resource-manager/bicep/file)
