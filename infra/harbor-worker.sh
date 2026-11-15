#!/usr/bin/env bash
# Run natively in a Railway Sandbox (or Linux VPS) with Docker and uv installed.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
export CRUCIBLE_ENVIRONMENTS=oncall
export RUNS_DIR="${RUNS_DIR:-$PWD/runs/worker}"
export HARBOR_JOBS_DIR="${HARBOR_JOBS_DIR:-$RUNS_DIR/jobs}"
export HARBOR_EXPORTS_DIR="${HARBOR_EXPORTS_DIR:-$RUNS_DIR/exports}"

case "${1:-}" in
  prepare)
    command -v uv >/dev/null
    docker info >/dev/null
    docker compose version
    uv tool install --python 3.13 'harbor==0.26.0'
    uv sync --directory engine --locked --no-dev
    mkdir -p "$HARBOR_JOBS_DIR" "$HARBOR_EXPORTS_DIR"
    if [ ! -f "$HARBOR_EXPORTS_DIR/oncall-demo/build-sidecar.sh" ]; then
      uv run --directory engine crucible export harbor --task oncall@1 \
        --seeds "${HARBOR_SEEDS:-0-2}" --out "$HARBOR_EXPORTS_DIR/oncall-demo"
    fi
    # Preserve existing salted exports; rebuilding the shared image is safe to repeat.
    (cd "$HARBOR_EXPORTS_DIR/oncall-demo" && sh build-sidecar.sh)
    ;;
  serve)
    : "${HARBOR_WORKER_TOKEN:?Set the same random token on the worker and public service}"
    : "${HARBOR_ALLOWED_MODELS:?Set a comma-separated list of approved provider/model IDs}"
    if [ "${#HARBOR_WORKER_TOKEN}" -lt 32 ]; then
      echo 'HARBOR_WORKER_TOKEN must contain at least 32 characters' >&2
      exit 1
    fi
    export HARBOR_WORKER_MODE=true
    export HARBOR_MAX_CONCURRENCY="${HARBOR_MAX_CONCURRENCY:-1}"
    export HARBOR_DAILY_JOB_LIMIT="${HARBOR_DAILY_JOB_LIMIT:-10}"
    export HARBOR_API_KEY_NAME="${HARBOR_API_KEY_NAME:-GOOGLE_GENERATIVE_AI_API_KEY}"
    export MODE=api
    unset HARBOR_WORKER_URL
    exec uv run --directory engine uvicorn --factory crucible.server.app:create_app \
      --host 0.0.0.0 --port "${PORT:-8080}"
    ;;
  *)
    echo 'Usage: bash infra/harbor-worker.sh prepare|serve' >&2
    exit 2
    ;;
esac
