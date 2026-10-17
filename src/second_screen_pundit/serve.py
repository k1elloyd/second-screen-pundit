"""The container's entry point: `python -m second_screen_pundit.serve`.

Logging and Azure Monitor are configured before the app is imported, so FastAPI and the Azure
SDK are instrumented from the first request. One process, one worker: replays, their outboxes
and the limits all live in memory.
"""

from __future__ import annotations

import os

import uvicorn

from second_screen_pundit.observability import configure_observability


def main() -> None:
    configure_observability()
    from second_screen_pundit.api import app

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        log_config=None,  # keep the root logging configured above
        access_log=os.getenv("ACCESS_LOG", "false").lower() == "true",
    )


if __name__ == "__main__":
    main()
