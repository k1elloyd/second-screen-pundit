# Copilot instructions: Second Screen Pundit

The full requirements are in `prompts/build-prompt-v2.md`. These non-negotiables apply to every session.

1. **Build only what this prompt describes.** It is the requirements document. Don't add features, pages or services it doesn't mention. If something seems missing, ask.
2. **Ship running code, not plans.** Never claim a command, deployment, model call or test passed without showing the output that proves it.
3. **Check before you design around something.** Verify Microsoft Agent Framework, Microsoft Foundry, Azure Speech, Azure Container Apps, azd and Bicep behaviour against current Microsoft Learn documentation before relying on it. Don't assume a preview feature, SKU, package name or API version exists. If the docs and this prompt disagree, stop and tell me.
4. **Deterministic code owns every number and every fact.** Analytics tools compute all statistics from the match event log. Models choose tools, decide what matters and explain results. They never compute, invent or override a statistic. Every number and every event a model output mentions must trace back to a tool result, and an automated check must prove it.
5. **All match data is synthetic.** Use fictional clubs, players and stadiums only. No real club, player, league, badge, crest or trademark appears anywhere: not in code, data, the UI, tests or the video. The one exception is the hackathon's own name, which may appear in the README and the video title. No match footage.
6. **Use keyless access everywhere.** Use one user-assigned managed identity in Azure and `DefaultAzureCredential` locally. Never write, log, print or commit keys, connection strings or tokens. The browser never receives any key or token: all speech-to-text and text-to-speech run on the server.
7. **Cost guardrails are features, not extras.** Every model call has `max_tokens`, a time budget and `store: False`. The public API is rate-limited per client. At most 6 replays run at once, and at most 3 from one IP address. Container Apps scale to zero.
8. **Keep it one container.** A single Python API serves the static front end, the event stream, the audio and the agents. Don't split it into microservices.
9. **Stop at each phase gate.** Report what was built, the evidence that it works (command output, test results) and anything you are unsure about. Then wait for me. Don't start the next phase on your own.
10. **Make routine engineering choices yourself.** Don't ask permission to write code, add a test or pick a reasonable library version.
