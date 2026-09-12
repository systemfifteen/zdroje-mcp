FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DATA_DIR=/data \
    TZ=Europe/Bratislava

# poppler-utils -> pdftotext; tzdata for cron times; curl for the healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

# supercronic: cron for containers (logs to stdout, no root daemon)
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_SHA1=71b0d58cc53f6bd72cf2f293e09e294b79c666d8
RUN curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && echo "${SUPERCRONIC_SHA1}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY sources.yaml crontab entrypoint.sh ./
RUN uv sync --frozen --no-dev && chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["/app/entrypoint.sh"]
