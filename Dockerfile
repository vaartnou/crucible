# syntax=docker/dockerfile:1
# Build from the repository root so both the UI and engine are available.
FROM node:22-bookworm-slim AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14-slim-bookworm AS engine-builder
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.14
WORKDIR /app
COPY engine/pyproject.toml engine/uv.lock engine/README.md ./
RUN uv sync --locked --no-dev --no-editable --no-install-project
COPY engine/src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime
ARG CRUCIBLE_GIT_COMMIT=unknown
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin crucible
WORKDIR /app
COPY --from=engine-builder /app/.venv /app/.venv
COPY --from=web-builder /web/dist /app/web/dist
ENV PATH=/app/.venv/bin:$PATH \
    PORT=8000 \
    MODE=api \
    CRUCIBLE_ENVIRONMENTS=oncall \
    RUNS_DIR=/tmp/runs \
    HARBOR_JOBS_DIR=/tmp/runs/harbor-jobs \
    HARBOR_EXPORTS_DIR=/tmp/runs \
    PYTHONUNBUFFERED=1 \
    CRUCIBLE_GIT_COMMIT=${CRUCIBLE_GIT_COMMIT}
USER crucible
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/healthz' % os.environ['PORT'], timeout=2)"]
CMD ["sh", "-c", "exec uvicorn --factory crucible.server.app:create_app --host 0.0.0.0 --port ${PORT}"]
