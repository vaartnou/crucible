"""Rebuild/verify the offline slice using ONLY the committed public world_v0 pack.

Run from any directory: python engine/scripts/build_cala_world_slice.py [--check].
No network, account credentials, Cala response archive, or private inputs are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

REVISION = "0bc537dc31d32e192aa06982be66e963b06e25e2"
PACK = "data/packs/cala/world_v0/"
DIRECT = [
    f"Supplier-{s}"
    for s in ("0001", "0040", "0042", "0043", "0078", "0089", "0092", "0106", "0119", "0131")
]
ALTERNATIVE = ["Supplier-0149", "Supplier-0056"]
REGION_MAP = {
    "north_america": "NA",
    "latin_america": "LATAM",
    "europe_west": "EU",
    "europe_east": "EU",
    "middle_east": "EU",
    "north_africa": "EU",
    "sub_saharan_africa": "EU",
    "east_asia_china": "APAC-N",
    "east_asia_other": "APAC-N",
    "south_asia": "APAC-S",
    "southeast_asia": "APAC-S",
    "oceania": "APAC-S",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]

    def read(name: str) -> bytes:
        return subprocess.check_output(["git", "show", f"{REVISION}:{PACK}{name}"], cwd=root)

    manifest = json.loads(read("manifest.json"))
    raw_facts, raw_sources = read("world_facts.json"), read("sources.json")
    for name, raw in (("world_facts.json", raw_facts), ("sources.json", raw_sources)):
        assert hashlib.sha256(raw).hexdigest() == manifest["files"][name], name
    facts = json.loads(raw_facts)
    aliases = set(DIRECT + ALTERNATIVE)
    selected = [
        f
        for f in facts
        if (
            f["subject_alias"] in aliases
            and f["kind"] == "supplier"
            and "Buyer-0001" in f["attributes"].get("supplies", [])
        )
        or (f["kind"] == "site" and f["attributes"].get("operator") in aliases)
    ]
    assert len([f for f in selected if f["kind"] == "supplier"]) == 12
    assert all(
        any(f["kind"] == "site" and f["attributes"]["operator"] == a for f in selected)
        for a in aliases
    )
    source_ids = {s for f in selected for s in f["source_ids"]}
    normalized: list[dict[str, Any]] = []
    for fact in selected:
        is_site = fact["kind"] == "site"
        alias = fact["subject_alias"]
        region = fact["region"]
        if region == "global":
            counts = Counter(
                REGION_MAP[f["region"]]
                for f in selected
                if f["kind"] == "site" and f["attributes"]["operator"] == alias
            )
            sim_region = max(sorted(counts), key=lambda region: counts[region])
        else:
            sim_region = REGION_MAP[region]
        normalized.append(
            {
                "id": fact["id"],
                "kind": "node",
                "alias": alias,
                "node_kind": "tier2" if is_site else ("direct" if alias in DIRECT else "alt"),
                "tier": 2 if is_site else 1,
                "region": region,
                "sim_region": sim_region,
                "industry": fact["industry"],
                "lat": None,
                "lon": None,
                "port": None,
                "lead_days": None,
                "price_index": None,
                "capacity_band": None,
                "supplies": [fact["attributes"]["operator"]]
                if is_site
                else fact["attributes"]["supplies"],
                "source_ids": fact["source_ids"],
                "confidence": "medium",
                "observation_kind": fact["kind"],
                "synthetic_fill": {
                    "node_kind": "scenario role; source establishes supplier link or ownership",
                    "tier": "scenario tier; site ownership is not an upstream supplier tier",
                    **(
                        {"supplies": "assumed operating output flows to its disclosed operator"}
                        if is_site
                        else {}
                    ),
                    **(
                        {"sim_region": "largest site-count region; global source has no HQ"}
                        if region == "global"
                        else {}
                    ),
                },
            }
        )
    result = {
        "schema": "supplygym-cala-slice-v1",
        "contract_version": "0.2",
        "source_revision": REVISION,
        "source_pack": "cala/world_v0",
        "source_pack_hash": manifest["pack_hash"],
        "buyer_alias": "Buyer-0001",
        "direct_aliases": DIRECT,
        "alternative_aliases": ALTERNATIVE,
        "facts": selected,
        "world_facts": normalized,
        "sources": [s for s in json.loads(raw_sources) if s["id"] in source_ids],
    }
    output = root / "engine/src/crucible/envs/supplygym/data/cala_world_v0.json"
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    if args.check:
        assert output.read_bytes() == data, "packaged slice differs; rebuild it"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    print(f"Verified {len(selected)} facts; slice SHA256 {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
