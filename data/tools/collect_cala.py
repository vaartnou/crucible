"""Run the Cala expansion query plan and cache responses in the private workspace."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    tool_dir = Path(__file__).resolve().parent
    plan = json.loads((tool_dir / "cala_expansion_queries.json").read_text(encoding="utf-8"))
    output = tool_dir.parents[1] / ".cala-private/expansion"
    output.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("CALA_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set CALA_API_KEY in the environment.")
    last_call = 0.0
    for index, (kind, question) in enumerate(plan, 1):
        if kind not in {"query", "search"}:
            raise ValueError(f"Unknown endpoint in query {index}")
        path = output / f"{index:02d}.json"
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior["question"] != question:
                raise ValueError(f"Cached question differs at {path}")
            if prior.get("status") == 200:
                continue
        request = Request(
            f"https://api.cala.ai/v1/knowledge/{kind}",
            data=json.dumps({"input": question}).encode(),
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        time.sleep(max(0, 6.5 - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        with urlopen(request, timeout=160) as response:
            packet = {
                "question": question, "status": response.status,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response": json.loads(response.read().replace(key.encode(), b"[redacted]")),
            }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        print(f"Saved query {index}/{len(plan)}")


if __name__ == "__main__":
    main()
