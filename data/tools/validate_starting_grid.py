"""Validate the frozen grid, provenance and rendered marker list without a network."""

import hashlib
import json
from pathlib import Path
import re
import sys

from probe_cala import ROOT
from build_starting_grid import clean, REGION, SIM


def validate(folder):
    def get(name):
        data = (folder / name).read_bytes()
        obj = json.loads(data)
        if (
            data
            != (
                json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
            ).encode()
        ):
            raise ValueError("Noncanonical JSON: " + name)
        return obj

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def ids(rows):
        keys = {r["id"] for r in rows}
        require(len(keys) == len(rows), "Duplicate id")
        return keys

    m = get("manifest.json")
    ns, es, ss = get("grid_nodes.json"), get("grid_edges.json"), get("sources.json")
    files = m["files"]
    require(
        set(files)
        == {
            p.name
            for p in folder.iterdir()
            if p.is_file() and p.name != "manifest.json"
        },
        "Manifest inventory mismatch",
    )
    for f, digest in files.items():
        require(
            hashlib.sha256((folder / f).read_bytes()).hexdigest() == digest,
            f + " hash mismatch",
        )
    require(
        hashlib.sha256(
            "\n".join(k + ":" + v for k, v in sorted(files.items())).encode()
        ).hexdigest()
        == m["pack_hash"],
        "Pack hash mismatch",
    )
    nodeids, edgeids, sourceids, queryids = (
        ids(ns),
        ids(es),
        ids(ss),
        ids(m["query_log"]),
    )
    require(
        not nodeids & edgeids and len({n["alias"] for n in ns}) == len(ns),
        "Duplicate id or alias",
    )
    leis = [n["lei"] for n in ns if n["node_type"] == "hq" and n["lei"]]
    require(len(leis) == len(set(leis)), "Duplicate company LEI")
    used = set()
    for r in ns + es:
        require(
            r["source_ids"] and set(r["source_ids"]) <= sourceids,
            "Missing provenance " + r["id"],
        )
        require(r["confidence"] in {"high", "medium", "low"}, "Invalid confidence")
        used.update(r["source_ids"])
    require(used == sourceids, "Orphan source")
    for s in ss:
        require(
            s["query_id"] in queryids
            and bool(re.fullmatch(r"src_cala_\d{6}", s["id"]))
            and bool(re.fullmatch("[0-9a-f]{64}", s["text_sha256"]))
            and s["url"].startswith(("https://", "http://")),
            "Invalid source",
        )
    report = (folder / "coverage.md").read_text(encoding="utf8")
    gaps = set(re.findall(r"^- (grid_\w+)$", report, re.M))
    expected = set()
    allowed = set(
        "hq factory warehouse distribution_centre supply_centre port airport_hub rail_terminal inland_port".split()
    )
    for n in ns:
        require(
            n["node_type"] in allowed
            and n["region"] == REGION[n["country"]]
            and n["sim_region"] == SIM[n["region"]],
            "Invalid geography/type",
        )
        for s in [n["alias"], n["city"], *(n["products"] or [])]:
            if s:
                require(clean(s) == s, "Unsanitised scalar")
        lat, lon = n["lat"], n["lon"]
        if lat is None:
            require(lon is None, "Unpaired coordinate")
            expected.add(n["id"])
        else:
            require(
                lon is not None
                and -90 <= lat <= 90
                and -180 <= lon <= 180
                and (lat, lon) == (round(lat, 1), round(lon, 1)),
                "Invalid coordinate",
            )
    require(gaps == expected, "Missing or spurious coordinate gap")
    for e in es:
        require(
            e["origin"] in nodeids and e["destination"] in nodeids,
            "Unresolved endpoint",
        )
        require(
            e["edge_type"] in {"supplies", "owns", "ships_via", "route"},
            "Invalid edge type",
        )
        require(
            e["mode"]
            in (
                {"air", "sea", "rail", "road"}
                if e["edge_type"] == "route"
                else {None, "air", "sea", "rail", "road"}
            ),
            "Invalid mode",
        )
        days = e["transit_days"]
        require(
            days is None or len(days) == 2 and 0 < days[0] <= days[1], "Invalid transit"
        )
    expected_counts = {"grid_nodes": len(ns), "grid_edges": len(es), "sources": len(ss)}
    require(m["counts"] == expected_counts, "Manifest counts")
    preview = (folder / "map.html").read_text(encoding="utf8")
    markers = json.loads(re.search(r"^const points = (.+);$", preview, re.M).group(1))
    require(
        markers
        == [[n["lat"], n["lon"], n["alias"]] for n in ns if n["lat"] is not None],
        "Map mismatch",
    )
    sample = (folder / "spot_check.txt").read_text(encoding="utf8").splitlines()
    require(
        len(sample) == min(20, len(ns) + len(es)) and all("http" in l for l in sample),
        "Spot-check sample",
    )
    hqs = [n for n in ns if n["node_type"] == "hq"]
    share = sum(n["region"].startswith("europe_") for n in hqs) / len(hqs)
    require(share >= 0.60, "HQ share below target")
    print(
        f"PASS {expected_counts}; mapped {len(markers)}; European HQ {share:.1%}; {m['pack_hash']}"
    )


if __name__ == "__main__":
    validate(
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else ROOT / "data/packs/cala/starting_grid_v0"
    )
