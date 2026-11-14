"""Validate the frozen Cala world fragment offline using the standard library."""

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path


DEFAULT = Path(__file__).resolve().parents[1] / "packs/cala/world_v0"
REGIONS = set("""east_asia_china east_asia_other south_asia southeast_asia
    europe_west europe_east middle_east north_africa sub_saharan_africa
    north_america latin_america oceania global""".split())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unique_ids(rows):
    ids = {r["id"] for r in rows}
    require(len(ids) == len(rows), "duplicate record ID")
    return ids


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(directory):
    def read(filename):
        return json.loads((directory / filename).read_bytes())

    manifest = read("manifest.json")
    files = manifest["files"]
    require(set(files) == {"sources.json", "world_facts.json"}, "unexpected manifest files")
    for name, expected in files.items():
        require(digest((directory / name).read_bytes()) == expected, f"file hash mismatch: {name}")
    lines = "\n".join(f"{name}:{files[name]}" for name in sorted(files))
    require(digest(lines.encode()) == manifest["pack_hash"], "pack hash mismatch")
    sources, facts = read("sources.json"), read("world_facts.json")
    require(manifest["counts"] == {"sources": len(sources), "world_facts": len(facts)}, "count mismatch")
    query_ids = unique_ids(manifest["query_log"])
    source_ids = unique_ids(sources)
    unique_ids(facts)
    for source in sources:
        require(source["query_id"] in query_ids and source["url"].startswith("https://")
                and re.fullmatch(r"[0-9a-f]{64}", source["text_sha256"]), source["id"])
    suppliers = {f["subject_alias"] for f in facts if f["kind"] == "supplier"}
    buyers = suppliers | {"Buyer-0001"}
    for f in facts:
        fid, kind, attrs = f["id"], f["kind"], f["attributes"]
        require(f["source_ids"] and set(f["source_ids"]) <= source_ids, fid)
        require(f["region"] in REGIONS and f["industry"] in {"electronics", "general_freight"}, fid)
        if kind == "supplier":
            require(attrs["supplies"] and set(attrs["supplies"]) <= buyers, fid)
            require(attrs.get("year") or attrs.get("observed_years"), fid)
        elif kind == "site":
            require(attrs["operator"] in suppliers and re.fullmatch(r"[A-Z]{2}", attrs["country"]), fid)
        elif kind == "route":
            require(re.fullmatch(r"\d{4}-\d{2}", attrs["schedule_month"]), fid)
            if attrs["mode"] == "sea":
                low, high = attrs["scheduled_transit_days"]
                require(0 < low <= high and attrs["service"], fid)
                code = r"[A-Z]{5}"
            else:
                require(attrs["mode"] == "air" and attrs["announced_weekly_frequency"] > 0, fid)
                code = r"[A-Z]{3}"
            require(all(re.fullmatch(code, p) for p in
                        [attrs["origin"], *attrs.get("via", []), attrs["destination"]]), fid)
        else:
            raise ValueError(f"unrecognized fact kind: {kind}")
    for kind in ("site", "route"):
        aliases = [f["subject_alias"] for f in facts if f["kind"] == kind]
        require(len(aliases) == len(set(aliases)), f"duplicate {kind} alias")
    counts = Counter(f["kind"] for f in facts)
    print(f"PASS: {len(sources)} sources, {len(facts)} facts, {dict(counts)}")


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        sys.exit(f"FAIL: {exc}")
