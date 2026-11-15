#!/bin/bash
# Harbor transfers trusted sidecar evidence into this fresh verifier container.
set -euo pipefail
exec python -m crucible.export.verify_episode
