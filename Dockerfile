FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTLINE_AGENT_CONFIG_PATH=/config/config.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md package.json package-lock.json ./
COPY docker/puppeteer-mermaid.json ./docker/puppeteer-mermaid.json
COPY src ./src

RUN uv sync --frozen --no-dev \
    && npm ci --ignore-scripts \
    && .venv/bin/python -c "import outline_agent; print('ok')"

RUN mkdir -p /config /data \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app /config /data

USER appuser

EXPOSE 8787
VOLUME ["/config", "/data"]

CMD ["/app/.venv/bin/outline-agent", "start", "--host", "0.0.0.0", "--port", "8787"]
