#!/bin/bash
# Read the grade from the sidecar and write the 0-1 reward for Harbor.
# GRADE_TOKEN comes from [verifier.env]; the agent never has it.
# Harbor reads the single score from reward.txt; diagnostics stay in grade.json.
mkdir -p /logs/verifier
python3 - <<'PY'
import json, math, os, time, urllib.request

out = "/logs/verifier"

def write(name, text):
    with open(os.path.join(out, name), "w") as f:
        f.write(text)

body, error = None, None
for _ in range(10):
    try:
        request = urllib.request.Request(
            "http://mcp-server:8000/grade",
            headers={"x-grade-token": os.environ.get("GRADE_TOKEN", "")},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
        break
    except Exception as exc:  # noqa: BLE001 - any failure is a zero reward, never a crash
        error = f"{type(exc).__name__}: {exc}"
        time.sleep(1)

if body is None:
    write("reward.txt", "0.0\n")
    write("grade_error.txt", error or "unknown error")
    print("grade failed:", error)
else:
    reward = float(body["reward"])
    reward = min(1.0, max(0.0, reward)) if math.isfinite(reward) else 0.0
    write("reward.txt", f"{reward}\n")
    summary = {k: body[k] for k in ("reward", "breakdown", "flags", "steps", "stop_reason")}
    write("grade.json", json.dumps(summary, indent=2))
    write("transcript.json", json.dumps(body["transcript"], indent=2))
    print(json.dumps(summary))
PY
