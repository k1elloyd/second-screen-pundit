# Second Screen Pundit

An explainable second-screen experience for fictional football matches.

## Build status

This repository is at **Phase 2**: deterministic synthetic matches, analytics tools, five Foundry agents in Agent Framework workflows, evidence validation, cached/off narrative modes, and server-side Azure Speech MP3 synthesis. The FastAPI application and deployable Azure infrastructure are later phases.

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

The evidence-check number extraction is adapted from Brian Benz's MIT-licensed [football-results-dashboard grounding.py](https://github.com/bbenz/football-results-dashboard/blob/main/agent/grounding.py); its license is retained in `third_party/bbenz_grounding/`.

Generated JSONL match logs and MP3 gate output are written under `data/matches/` and `data/audio/`, and are ignored by Git. The azd and Bicep files are still scaffolding only; resource provisioning and a container build are added in a later phase.
