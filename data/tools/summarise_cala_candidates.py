"""Build a private discovery inventory from cached regional Cala query responses.

Rows remain unverified candidates. Headquarters countries are not operating sites;
query-response hashes establish collection traceability, not factual provenance.
"""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata


ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".cala-private"
INPUT = PRIVATE / "regional_discovery"
OUTPUT = PRIVATE / "candidate_inventory.json"


def normalise(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value)
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", value).casefold())


def main() -> None:
    candidates = {}
    batches = {}
    errors = []
    raw_rows = 0
    for path in sorted(INPUT.glob("*_query.json")):
        response = json.loads(path.read_text(encoding="utf-8"))
        aliases = {}
        for entity in response.get("entities") or []:
            if entity.get("entity_type") not in ("Company", "Organization"):
                continue
            for alias in [entity.get("name", ""), *(entity.get("mentions") or [])]:
                if isinstance(alias, str) and alias:
                    aliases.setdefault(normalise(alias), set()).add(entity["id"])
        added = 0
        for row in response.get("results") or []:
            if not isinstance(row, dict) or "error" in row:
                errors.append({"response_file": path.name, "reason": "error_or_invalid_row"})
                continue
            name = next((row.get(k) for k in ("company_name", "company", "name")
                         if isinstance(row.get(k), str) and row[k].strip()), None)
            if not name:
                errors.append({"response_file": path.name, "reason": "missing_company_name"})
                continue
            raw_rows += 1
            ids = sorted(aliases.get(normalise(name), set()))
            identity = "entity:" + ids[0] if len(ids) == 1 else "name:" + normalise(name)
            entry = candidates.setdefault(identity, {
                "name": name, "candidate_entity_ids": ids,
                "status": "unverified_candidate", "observations": [],
            })
            entry["observations"].append({
                "response_file": str(path.relative_to(PRIVATE)).replace("\\", "/"),
                "response_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "row": row,
            })
            added += 1
        batches[path.stem] = added
    country_counts = Counter()
    for entry in candidates.values():
        row = entry["observations"][0]["row"]
        country = row.get("headquarters_country", row.get("hq_country", row.get("country")))
        country_counts[country if isinstance(country, str) else "unspecified"] += 1
    summary = {
        "candidate_rows": raw_rows, "deduplicated_candidates": len(candidates),
        "candidates_with_one_entity_match": sum(
            len(e["candidate_entity_ids"]) == 1 for e in candidates.values()),
        "batches": batches, "reported_headquarters_countries": dict(sorted(country_counts.items())),
        "validated_world_facts": 0, "errors": errors,
    }
    result = {"summary": summary, "candidates": [candidates[k] for k in sorted(candidates)]}
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("Private inventory:", OUTPUT)


if __name__ == "__main__":
    main()
