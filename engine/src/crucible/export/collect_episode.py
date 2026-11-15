"""Freeze sidecar evidence after Harbor stops the agent, before artifact collection."""

import json
import os
import urllib.request
from pathlib import Path

EVIDENCE_PATH = Path("/tmp/verification/episode.json")


def collect_episode(path: Path = EVIDENCE_PATH) -> None:
    """Close the episode through the authenticated local endpoint and save it atomically."""
    request = urllib.request.Request(
        "http://127.0.0.1:8000/grade",
        headers={"x-grade-token": os.environ["GRADE_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, allow_nan=False) + "\n")
    temporary.replace(path)


if __name__ == "__main__":
    collect_episode()
