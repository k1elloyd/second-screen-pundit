# Second Screen Pundit

An explainable second-screen experience for fictional football matches.

## Build status

This repository is at **Phase 3**: deterministic synthetic matches, five Foundry agents, evidence validation, cached/off narrative modes, server-side Azure Speech, a FastAPI API with an SSE replay stream, and a single-page second-screen UI. Azure deployment and infrastructure remain later phases.

## Local setup

Create the Python 3.12 environment before installing packages:

```powershell
py -3.12 -m venv .venv
```

Install the exact versions declared in `pyproject.toml`:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the Foundry project endpoint, deployment names, and Azure Speech custom endpoint and region. Do not commit `.env`. Local Azure access uses `DefaultAzureCredential`; sign in with `az login` first.

Run the offline test suite (live Azure tests are excluded by default):

```powershell
.\.venv\Scripts\python.exe -m pytest
```

After setting the local Foundry values, run the Phase 0 live gate. It makes one short chat request and one function-tool round trip, both with `store: False`:

```powershell
.\.venv\Scripts\python.exe -m pytest -s -m live tests\test_foundry_live.py
```

Run the console Commentator against the deterministic late-comeback match at 60x:

```powershell
.\.venv\Scripts\python.exe -m second_screen_pundit.console m01 --speed 60
```

Run the Phase 2 gate. It prints one cited transcript and writes a Speech-generated MP3 after verifying its duration is at least two minutes:

```powershell
.\.venv\Scripts\python.exe -m second_screen_pundit.agents.gate
```

## Run the Phase 3 API and web UI

Start the one-container API and static front end locally. The app begins the Foundry warm-up in the background; the Start button is enabled after `/healthz` reports readiness.

```powershell
.\.venv\Scripts\python.exe -m uvicorn second_screen_pundit.api:app --host 127.0.0.1 --port 8000 --env-file .env
```

Open `http://127.0.0.1:8000`, select **m02**, set the speed to **20×**, and start the replay. During play, submit one typed question and one push-to-talk question, open **Why?** on a commentary line, then let the match reach full time to see the recap and its audio. The viewer questions are cut off at the latest event released to that browser, and question limits are shared between typed and spoken requests.

Commentary text is sent before its audio is synthesized, so a slow speech service does not hide the line or hold up full time. If a model or speech call fails, the app uses a deterministic commentary fallback or shows text without audio. Interactive model calls default to 8 seconds for live commentary and 10 seconds for questions; `LIVE_WINDOW_TIMEOUT_SECONDS`, `EXPLAINER_TIMEOUT_SECONDS`, and `SPEECH_SYNTHESIS_TIMEOUT_SECONDS` can override those budgets.

The evidence-check number extraction is adapted from Brian Benz's MIT-licensed [football-results-dashboard grounding.py](https://github.com/bbenz/football-results-dashboard/blob/main/agent/grounding.py); its license is retained in `third_party/bbenz_grounding/`.

Generated JSONL match logs and MP3 gate output are written under `data/matches/` and `data/audio/`, and are ignored by Git. The azd and Bicep files are still scaffolding only; resource provisioning and a container build are added in a later phase.
