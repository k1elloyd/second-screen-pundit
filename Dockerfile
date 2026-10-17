# One container: the API, the static page, the event stream, the audio and the agents.
# Debian 12 is a Speech SDK supported distribution; it needs libssl3, libasound2 and CA certs.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The Speech SDK caches its downloads under TMPDIR; audio is made in a temp dir too.
    TMPDIR=/tmp \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY third_party ./third_party
COPY src ./src
RUN pip install .

# Cached narrative for cached mode; read relative to the working directory.
COPY data/cached ./data/cached

RUN useradd --system --uid 10001 --no-create-home pundit
USER pundit

EXPOSE 8000
# One worker: replays, their outboxes and the limits are held in this process's memory.
CMD ["python", "-m", "second_screen_pundit.serve"]
