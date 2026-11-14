"""Run at most three Cala requests; cache responses privately and print metadata only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
BASE = "https://api.cala.ai"


def main() -> None:
    key = os.environ.get("CALA_API_KEY", "").strip()
    if not key:
        raise SystemExit("CALA_API_KEY is missing; configure it locally, never in chat.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / ".cala-private" / "probes" / stamp
    out.mkdir(parents=True, exist_ok=False)
    log: list[dict] = []
    last_call = 0.0

    def call(method: str, path: str, payload: dict | None = None) -> dict:
        nonlocal last_call
        if len(log) >= 3:
            raise SystemExit("Three-call probe limit reached.")
        time.sleep(max(0.0, 6.2 - (time.monotonic() - last_call)))
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            BASE + path,
            data=body,
            method=method,
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        record = {
            "id": f"q{len(log) + 1:03}", "method": method, "path": path,
            "payload": payload, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        log.append(record)
        last_call = time.monotonic()
        try:
            with urlopen(request, timeout=90) as response:
                raw = response.read()
                record["status"] = response.status
        except HTTPError as error:
            raw = error.read()
            record["status"] = error.code
        except (URLError, TimeoutError):
            record["status"] = "transport_error"
            (out / "query_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
            raise SystemExit("Transport failure; no automatic retry. Key not displayed.") from None
        # Defensive redaction in case an upstream error unexpectedly echoes auth data.
        raw = raw.replace(key.encode("utf-8"), b"[REDACTED]")
        raw_path = out / f"{record['id']}.json"
        raw_path.write_bytes(raw)
        record["response_sha256"] = hashlib.sha256(raw).hexdigest()
        (out / "query_log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
        print(f"{record['id']}: HTTP {record['status']}; {len(raw)} bytes cached privately", flush=True)
        if record["status"] != 200:
            raise SystemExit("Probe stopped on non-200 response; no automatic retry.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise SystemExit("Unexpected response shape; inspect private cache before continuing.")
        return result

    search = call("GET", "/v1/entities?" + urlencode({
        "name": "Hon Hai Precision Industry", "entity_types": "Company", "limit": 5,
    }))
    matches = [x for x in search.get("entities", []) if x.get("entity_type") == "Company"]
    plausible = [x for x in matches if re.search(r"hon hai|foxconn", x.get("name", ""), re.I)]
    if not plausible:
        raise SystemExit("No matching company resolved; stopped without guessing an entity.")
    entity = max(plausible, key=lambda x: "hon hai precision" in x.get("name", "").lower())
    entity_id = str(UUID(entity["id"]))
    schema = call("GET", f"/v1/entities/{entity_id}/introspection")
    properties = [p for p in schema.get("properties", []) if isinstance(p, str) and re.search(
        r"name|address|country|location|employee|revenue|industry|product|capacity|site|facility|found", p, re.I
    )]
    relationships = {}
    for direction in ("incoming", "outgoing"):
        kinds = schema.get("relationships", {}).get(direction, [])
        useful = [k for k in kinds if isinstance(k, str) and re.search(
            r"SUPPL|CUSTOMER|MANUFACT|PRODUC|FACIL|OPERAT|LOCAT|HEADQUART|REGISTER|PARENT|SUBSIDIARY", k, re.I
        )]
        if useful:
            relationships[direction] = {k: {"limit": 5, "offset": 0} for k in useful}
    payload = {"properties": properties, "relationships": relationships}
    if not properties and not relationships:
        raise SystemExit("No relevant fields discovered; stopped before retrieval.")
    profile = call("POST", f"/v1/entities/{entity_id}", payload)
    print("Available property names:", json.dumps(schema.get("properties", [])), flush=True)
    print("Available relationship types:", json.dumps(schema.get("relationships", {})), flush=True)
    print("Retrieved property names:", json.dumps(list(profile.get("properties", {}))), flush=True)
    counts = {
        direction: {kind: len(rows) for kind, rows in kinds.items() if isinstance(rows, list)}
        for direction, kinds in profile.get("relationships", {}).items() if isinstance(kinds, dict)
    }
    print("Retrieved relationship counts:", json.dumps(counts), flush=True)

    def count_sources(value):
        if isinstance(value, dict):
            return sum(len(v) if k == "sources" and isinstance(v, list) else count_sources(v)
                       for k, v in value.items())
        if isinstance(value, list):
            return sum(count_sources(v) for v in value)
        return 0

    print("Source entries returned:", count_sources(profile), flush=True)
    print("Calls made:", len(log), "; credits charged must be checked in the console", flush=True)
    print("Private cache:", out, flush=True)


if __name__ == "__main__":
    main()
