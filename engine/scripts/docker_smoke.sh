#!/usr/bin/env bash
# Build the engine image and smoke-test it: one full episode on /v1, and the sidecar grade token.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-crucible-engine:smoke}"
COMMIT="${CRUCIBLE_GIT_COMMIT:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
TOKEN="smoke-test-token"
CONTAINERS=()

cleanup() {
  for c in "${CONTAINERS[@]:-}"; do
    [ -n "$c" ] && docker rm -f "$c" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

wait_healthy() {
  local port=$1
  for _ in $(seq 1 60); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${port}/healthz")" = "200" ]; then
      return 0
    fi
    sleep 0.5
  done
  echo "server on port ${port} did not become healthy" >&2
  return 1
}

echo "== build $IMAGE (commit $COMMIT)"
docker build --build-arg "CRUCIBLE_GIT_COMMIT=${COMMIT}" -t "$IMAGE" .

echo "== non-root check"
uid="$(docker run --rm --entrypoint id "$IMAGE" -u)"
[ "$uid" != "0" ] || { echo "image runs as root" >&2; exit 1; }

echo "== api mode"
PORT_API="$(free_port)"
CONTAINERS+=("$(docker run -d -p "${PORT_API}:8000" "$IMAGE")")
wait_healthy "$PORT_API"
OUT="$(uv run crucible play --api "http://localhost:${PORT_API}" --task oncall@1 --seed 0 --policy reference)"
echo "$OUT"
python3 - "$OUT" <<'PY'
import json, sys
score = json.loads(sys.argv[1])["score"]
assert score > 0.9, f"reference score {score} is not above 0.9"
PY

echo "== sidecar mode"
PORT_SIDE="$(free_port)"
CONTAINERS+=("$(docker run -d -p "${PORT_SIDE}:8000" \
  -e MODE=sidecar -e "GRADE_TOKEN=${TOKEN}" -e SIDECAR_TASK=oncall@1 -e SIDECAR_SEED=0 "$IMAGE")")
wait_healthy "$PORT_SIDE"
NO_TOKEN="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT_SIDE}/grade")"
WITH_TOKEN="$(curl -s -o /dev/null -w '%{http_code}' -H "x-grade-token: ${TOKEN}" "http://localhost:${PORT_SIDE}/grade")"
echo "grade without token: ${NO_TOKEN}, with token: ${WITH_TOKEN}"
[ "$NO_TOKEN" = "403" ] || { echo "expected 403 without token" >&2; exit 1; }
[ "$WITH_TOKEN" = "200" ] || { echo "expected 200 with token" >&2; exit 1; }

echo "smoke test passed"
