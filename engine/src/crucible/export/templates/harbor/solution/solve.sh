#!/bin/bash
# Harbor supplies this token only to the oracle solution.
set -euo pipefail
curl --fail --silent --show-error --max-time 120 -X POST \
  -H "x-solution-token: ${SOLUTION_TOKEN:?missing solution token}" \
  http://mcp-server:8000/autoplay
