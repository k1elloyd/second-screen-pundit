# Second Screen Pundit

An explainable second-screen experience for fictional football matches.

## Build status

This repository is at **Phase 1**: deterministic synthetic matches, event-log replay, analytics tools, and a console Commentator. The API, other agents, speech, and deployable Azure infrastructure are not included yet.

## Local setup

Create the Python 3.12 environment before installing packages:

```powershell
py -3.12 -m venv .venv
```

Install the exact versions declared in `pyproject.toml`:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the Foundry project endpoint and deployment names. Do not commit `.env`. Local Azure access uses `DefaultAzureCredential`; sign in with `az login` first.

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

Generated JSONL match logs are written under `data/matches/` and are ignored by Git. The azd and Bicep files are still scaffolding only; resource provisioning and a container build are added in a later phase.
