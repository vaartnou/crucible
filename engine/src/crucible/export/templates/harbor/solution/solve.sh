#!/bin/bash
# Oracle: play a scripted Crucible policy against the sidecar over MCP.
# Harbor uploads solution/ only for the oracle agent, so agent runs never see the engine.
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
POLICY="${CRUCIBLE_POLICY:-reference}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/@@UV_VERSION@@/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv venv /tmp/crucible-solution --python 3.14 --quiet
uv pip install --python /tmp/crucible-solution --quiet \
  -r "$SOLUTION_DIR/requirements.txt" "$SOLUTION_DIR/@@WHEEL_NAME@@"

# Oracle entry point (replaced by the Cala live probe).
/tmp/crucible-solution/bin/crucible play --mcp http://mcp-server:8000/mcp --policy "$POLICY" \
  --task "@@TASK_REF@@"
