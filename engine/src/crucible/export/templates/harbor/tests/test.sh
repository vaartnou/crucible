#!/bin/bash
# Read the grade from the sidecar and write the 0-1 reward for Harbor.
# GRADE_TOKEN comes from [verifier.env]; the agent never has it.
# The breakdown goes to grade.json: Harbor reads reward.json as a dict of numeric rewards.
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
    write("reward.json", json.dumps(dict.fromkeys(("reward", "impact", "verified", "diagnosis", "penalty"), 0.0)))
    write("grade_error.txt", error or "unknown error")
    print("grade failed:", error)
else:
    reward = min(1.0, max(0.0, float(body["reward"])))
    breakdown, flags = body.get("breakdown", {}), body.get("flags", {})
    def number(value):
        value = float(value)
        return value if math.isfinite(value) else 0.0
    rewards = {"reward": number(reward)}
    for key in ("impact", "verified", "diagnosis", "penalty"):
        rewards[key] = number(breakdown.get(key, flags.get(key, 0.0)))
    write("reward.json", json.dumps(rewards))
    summary = {k: body[k] for k in ("reward", "breakdown", "flags", "steps", "stop_reason")}
    write("grade.json", json.dumps(summary, indent=2))
    write("transcript.json", json.dumps(body["transcript"], indent=2))
    print(json.dumps(summary))
PY
