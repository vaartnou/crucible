"""Build the starting grid offline from reviewed Cala contexts and scoped registry matches."""

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import random
import re
import unicodedata
from urllib.parse import urlsplit

from probe_cala import ROOT
from grid_entities import locality
from grid_geography import reference_files, unique_city, fold

CACHE = ROOT / ".cala-private/grid"
OUTPUT = ROOT / "data/packs/cala/starting_grid_v0"
REGIONS = {
    "europe_west": "AT BE CH DE DK ES FI FR GB GR IE IS IT LU NL NO PT SE",
    "europe_east": "AL BA BG BY CZ EE HR HU LT LV MD ME MK PL RO RS RU SI SK TR UA",
    "east_asia_china": "CN HK",
    "east_asia_other": "JP KR TW",
    "southeast_asia": "ID MY PH SG TH VN",
    "south_asia": "IN BD",
    "north_america": "US CA",
    "latin_america": "BR AR CL MX PA",
    "middle_east": "AE IL OM SA QA",
    "oceania": "AU NZ",
    "north_africa": "DZ EG MA TN",
    "sub_saharan_africa": "ZA KE NG GH",
}
REGION = {c: r for r, codes in REGIONS.items() for c in codes.split()}
SIM = dict(
    europe_east="EU",
    europe_west="EU",
    east_asia_china="APAC-N",
    east_asia_other="APAC-N",
    southeast_asia="APAC-S",
    south_asia="APAC-S",
    north_america="NA",
    latin_america="LATAM",
    middle_east="EU",
    oceania="APAC-S",
    north_africa="EU",
    sub_saharan_africa="EU",
)
RISK = re.compile(
    r"(?:previous instructions|system prompt|\bignore\b|\byou must\b|\bassistant\b|\btool\b|call the|output the|<[^>]+>|https?://|\S+@\S+|\x60{3}|[\x00-\x1f])",
    re.I,
)


def clean(value):
    if not isinstance(value, str) or RISK.search(value) or len(value) > 160:
        raise ValueError("unsafe or oversized scalar")
    value = html.unescape(value)
    if RISK.search(value):
        raise ValueError("unsafe decoded scalar")
    # NFKD alone drops these letters entirely (e.g. Lodz becomes Odz).
    value = value.translate(
        str.maketrans(
            {
                "Ł": "L",
                "ł": "l",
                "Ø": "O",
                "ø": "o",
                "Đ": "D",
                "đ": "d",
                "ß": "ss",
                "Æ": "AE",
                "æ": "ae",
                "Œ": "OE",
                "œ": "oe",
            }
        )
    )
    return (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().strip()
    )


def key(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())


def norm(value):
    value = re.sub(r"\([^)]*\)", "", html.unescape(value).casefold())
    value = re.sub(
        r"\b(incorporated|inc|corporation|corp|limited|ltd|co|company|ag|plc|pcl|nv|gmbh)\b",
        "",
        value,
    )
    return "".join(c for c in value if c.isalnum())


def read(path):
    return json.loads(path.read_bytes())


def encode(data):
    return (
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(archive, out, public=False):
    old_alias = read(archive / "disclosure_alias_map.json")
    work_alias = dict(old_alias)
    nodes, links, sources, logs = {}, {}, {}, {}
    reject = Counter()
    coordinate = {}
    places, geo, ports = reference_files(CACHE.parent / "public")
    public_evidence = {}
    public_points = {}
    public_meta = {}

    def verified(ident, url, raw, provider):
        digest = sha(raw) if isinstance(raw, bytes) else raw
        public_evidence.setdefault(ident, set()).add((url, digest, provider))

    named = {}

    def alias(name):
        clean(name)
        k = norm(name)
        if k not in work_alias:
            work_alias[k] = "Company-" + sha(k.encode())[:14]
        return work_alias[k]

    def source(packet, evidence, url):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or re.search(r"[\s<>\\]", url)
        ):
            raise ValueError("invalid citation URL")
        question = packet["question"]
        qid = "q_grid_" + sha(question.encode())[:12]
        qdate = packet["retrieved_at"]
        logs[qid] = dict(
            id=qid,
            endpoint=(
                "POST /v1/entities/{id}"
                if packet.get("endpoint") == "entity"
                else "POST /v1/knowledge/" + packet.get("endpoint", "search")
            ),
            params={"input_sha256": sha(question.encode())},
            n_results=1
            if packet.get("endpoint") == "entity"
            else len(
                packet["response"].get("results")
                or packet["response"].get("context")
                or []
            ),
            retrieved_at=qdate,
        )
        digest = sha(evidence.encode())
        sid = (url, digest, qid)
        sources[sid] = dict(
            provider="cala",
            url=url,
            publisher=parts.hostname,
            published_at=None,
            query_id=qid,
            retrieved_at=qdate[:10],
            title="Cited source record",
            text_sha256=digest,
        )
        return sid

    def add_node(kind, label, city, country, refs, **extras):
        if kind == "hq" and not city:
            raise ValueError("empty normalized headquarters city")
        if country not in REGION or not refs:
            raise ValueError("unsupported country or missing citation")
        ident = "grid_" + sha((kind + ":" + label).encode())[:16]
        n = nodes.setdefault(
            ident,
            dict(
                id=ident,
                alias=label,
                node_type=kind,
                city=city,
                country=country,
                region=REGION[country],
                sim_region=SIM[REGION[country]],
                lat=None,
                lon=None,
                industry=None,
                industry_detail=None,
                products=None,
                capacity_band=None,
                size_band=None,
                public_private=None,
                registry_number=None,
                vat_number=None,
                opened_year=None,
                status=None,
                owner_alias=None,
                operator_alias=None,
                lei=None,
                throughput_band=None,
                throughput_year=None,
                confidence="medium",
                source_ids=set(),
            ),
        )
        if n["country"] != country or (
            n["city"] and city and key(n["city"]) != key(city)
        ):
            raise ValueError("conflicting location")
        n.update({k: v for k, v in extras.items() if v is not None})
        n["source_ids"].update(refs)
        return ident

    def add_edge(kind, a, b, refs, mode=None, observed=None, transit=None):
        ident = "edge_" + sha(encode([kind, a, b, mode, observed]))[:16]
        if ident in links:
            links[ident]["source_ids"].update(refs)
            return ident
        links[ident] = dict(
            id=ident,
            edge_type=kind,
            origin=a,
            destination=b,
            mode=mode,
            transit_days=transit,
            observed=observed,
            source_ids=set(refs),
            confidence="medium",
        )

        return ident

    def context_refs(packet, ids):
        refs = []
        for ctx in packet["response"].get("context") or []:
            if ctx["id"] in ids:
                for origin in ctx.get("origins") or []:
                    url = (origin.get("document") or {}).get("url")
                    if url:
                        refs.append(source(packet, ctx["content"], url))
        return refs

    reviews = read(CACHE / "reviewed.json")
    for rec in reviews:
        packet = read(
            (archive / "expansion" if rec["legacy"] else CACHE) / rec["packet"]
        )
        refs = context_refs(packet, rec["contexts"])
        if not refs:
            reject["selection without a source"] += 1
            continue
        kind = rec["kind"]
        if kind == "coord":
            coordinate[rec["country"], key(rec["city"])] = (
                rec["lat"],
                rec["lon"],
                refs,
            )
            continue
        if kind == "route":
            add_edge(
                kind,
                named[rec["origin"]],
                named[rec["destination"]],
                refs,
                rec["mode"],
                rec.get("observed"),
            )
            continue
        city = clean(rec["city"]) if rec["city"] else None
        label = (
            alias(rec["name"])
            if kind == "hq"
            else (
                "Site-" + sha(rec["name"].encode())[:12]
                if kind == "factory"
                else clean(rec["name"])
            )
        )
        named[rec["name"]] = add_node(
            kind,
            label,
            city,
            rec["country"],
            refs,
            industry=rec.get("industry")
            or ("general_freight" if kind in {"port", "rail_terminal"} else None),
            operator_alias=alias(rec["operator"]) if rec.get("operator") else None,
        )

    # Intersect the cached review with separately verified records in the earlier public fragment.
    prior = read(ROOT / "data/packs/cala/world_v0/world_facts.json")
    origins = {s["id"]: s for s in read(ROOT / "data/packs/cala/world_v0/sources.json")}
    for n in nodes.values():
        if n["node_type"] == "factory":
            for fact in prior:
                a = fact["attributes"]
                if (
                    fact["kind"] == "site"
                    and a.get("city") == n["city"]
                    and a.get("country") == n["country"]
                    and a.get("operator") == n["operator_alias"]
                ):
                    old = origins[fact["source_ids"][0]]
                    verified(
                        n["id"], old["url"], old["text_sha256"], "public_disclosure"
                    )
                    public_meta[n["id"]] = {"industry": "electronics"}
            if n["city"] == "Higashiomi":
                raw = (CACHE.parent / "public/yokaichi.html").read_bytes()
                if (
                    b"Higashiomi" in raw
                    and b"1962" in raw
                    and b"ceramic raw materials" in raw
                ):
                    for url, _, _ in n["source_ids"]:
                        if url.endswith("/branch/yokaichi"):
                            verified(n["id"], url, raw, "public_disclosure")
                            n.update(
                                opened_year=1962,
                                products=["ceramic raw materials"],
                                industry="electronics",
                            )
                            public_meta[n["id"]] = {
                                "opened_year": 1962,
                                "products": n["products"],
                                "industry": "electronics",
                            }

    for item in read(CACHE / "reviewed_factories.json"):
        for n in nodes.values():
            if (
                n["node_type"] == "factory"
                and n["city"] == item["city"]
                and n["operator_alias"] == alias(item["owner"])
            ):
                verified(
                    n["id"],
                    item["url"],
                    (CACHE.parent / "public" / item["doc"]).read_bytes(),
                    "public_disclosure",
                )
                public_meta.setdefault(n["id"], {})["industry"] = n["industry"]
    # Independently match reviewed network rows to the exact Cala context, never just an external URL.
    for row in read(archive / "network_review.json")["relationships"]:
        if row.get("cala_query") and row["relation"] == "manufactures_at":
            packet = read(archive / "network_evidence" / (row["cala_query"] + ".json"))
            refs = context_refs(packet, [row["context_id"]])
            city, country = row["object"].rsplit(", ", 1)
            label = "Site-" + sha((row["subject"] + " " + city).encode())[:12]
            product = row.get("product")
            # Product descriptions containing brands stay private.
            add_node(
                "factory",
                label,
                clean(city),
                country,
                refs,
                operator_alias=alias(row["subject"]),
                products=[clean(product)]
                if product
                in {
                    "SiC substrates",
                    "smartphone image sensors",
                    "retail and warehouse automation equipment",
                }
                else None,
            )

    for item in read(CACHE / "reviewed_hubs.json"):
        packet = read(CACHE / item["packet"])
        refs = context_refs(packet, item["contexts"])
        if not refs:
            reject["hub without a specific source"] += 1
            continue
        ident = add_node(
            item["kind"],
            clean(item["name"]),
            clean(item["city"]),
            item["country"],
            refs,
            industry="general_freight",
        )
        entry = ports.get(item["code"])
        if entry and entry["Country"] == item["country"]:
            # UN/LOCODE confirms the city's transport function. A cargo airport or
            # inland facility also needs an independent operator document below.
            if item["kind"] == "port" and entry["Function"].startswith("1"):
                verified(
                    ident,
                    "https://service.unece.org/trade/locode/"
                    + item["country"].lower()
                    + ".htm",
                    json.dumps(entry, sort_keys=True).encode(),
                    "unlocode",
                )
        if item.get("public_doc"):
            doc = CACHE.parent / "public" / item["public_doc"]
            verified(ident, item["public_url"], doc.read_bytes(), "public_disclosure")

    for item in read(CACHE / "reviewed_rows.json"):
        packet = read(CACHE / item["packet"])
        row = next(
            r
            for r in packet["response"]["results"]
            if r.get(item["field"]) == item["value"]
            and r.get("source_url") == item["url"]
        )
        if item["country"] != row.get("country_code") or clean(row["city"]) != clean(
            item["city"]
        ):
            raise ValueError("Reviewed row changed")
        label = "Site-" + sha(item["value"].encode())[:12]
        refs = [source(packet, json.dumps(row, sort_keys=True), item["url"])]
        ident = add_node(
            item["kind"], label, clean(item["city"]), item["country"], refs
        )
        raw = (CACHE.parent / "public" / item["doc"]).read_bytes()
        verified(ident, item["url"], raw, "public_disclosure")
        if item.get("owner_verified"):
            nodes[ident]["owner_alias"] = alias(row["owner"])

    for item in read(CACHE / "reviewed_sites.json"):
        packet = read(CACHE / item["packet"])
        refs = context_refs(packet, item["contexts"])
        if not refs:
            continue
        label = "Site-" + sha(item["label"].encode())[:12]
        ident = add_node(
            item["kind"],
            label,
            clean(item["city"]),
            item["country"],
            refs,
            operator_alias=alias(item["operator"]) if item.get("operator") else None,
            owner_alias=alias(item["owner"]) if item.get("owner") else None,
            products=[clean(v) for v in item["products"]]
            if item.get("products")
            else None,
            industry=item.get("industry"),
            opened_year=item.get("opened_year"),
        )
        if item.get("doc"):
            verified(
                ident,
                item["url"],
                (CACHE.parent / "public" / item["doc"]).read_bytes(),
                "public_disclosure",
            )
            public_meta[ident] = {"products": nodes[ident]["products"]}

    def hq_place(name, value, refs):
        # If two municipality names appear in the address, the registry's city
        # field disambiguates them only if the same city is present in Cala.
        for url, _, _ in refs:
            lei = url.rsplit("/", 1)[-1]
            if not re.fullmatch("[A-Z0-9]{18}[0-9]{2}", lei):
                continue
            file = CACHE.parent / "registry" / (lei + ".json")
            if not file.is_file():
                continue
            entity = read(file).get("data", {}).get("attributes", {}).get("entity", {})
            addr = entity.get("headquartersAddress") or {}
            city = addr.get("city") or ""
            country = addr.get("country") or ""
            if country not in REGION or fold(name) != fold(
                entity.get("legalName", {}).get("name", "")
            ):
                continue
            if (
                city
                and fold(city) in {fold(t.strip()) for t in value.split(",")}
                and value.split(",")[-1].strip(". ").upper() == country
            ):
                return city.title(), country
        return locality(value, places, REGION)

    def registry_public(name, ident):
        n = nodes[ident]
        for url, _, _ in list(n["source_ids"]):
            lei = url.rsplit("/", 1)[-1]
            path = CACHE.parent / "registry" / (lei + ".json")
            if not re.fullmatch("[A-Z0-9]{18}[0-9]{2}", lei) or not path.exists():
                continue
            entity = read(path).get("data", {}).get("attributes", {}).get("entity", {})
            addr = entity.get("headquartersAddress", {})
            if (
                fold(entity.get("legalName", {}).get("name", "")) == fold(name)
                and fold(n["city"]) == fold(addr.get("city", ""))
                and n["country"] == addr.get("country")
            ):
                verified(
                    ident,
                    "https://search.gleif.org/#/record/" + lei,
                    path.read_bytes(),
                    "gleif",
                )
                n["lei"] = lei

    # Registry checks supplement a Cala citation; an uncited discovery row cannot pass this gate.
    for path in sorted(CACHE.glob("*.json")):
        if not re.fullmatch("[0-9a-f]{64}", path.stem):
            continue
        packet = read(path)
        if packet.get("endpoint") == "search":
            contexts = {c["id"]: c for c in packet["response"].get("context") or []}
            for claim in packet["response"].get("explainability") or []:
                sentence = claim.get("content", "")
                if not re.search(r"headquarter|head office|\bHQ\b", sentence, re.I):
                    continue
                for ref in claim.get("references") or []:
                    ctx = contexts.get(ref, {})
                    for origin in ctx.get("origins") or []:
                        url = (origin.get("document") or {}).get("url", "")
                        lei = url.rsplit("/", 1)[-1]
                        if not re.fullmatch("[A-Z0-9]{18}[0-9]{2}", lei):
                            continue
                        reg = CACHE.parent / "registry" / (lei + ".json")
                        if not reg.is_file():
                            continue
                        entity = (
                            read(reg)
                            .get("data", {})
                            .get("attributes", {})
                            .get("entity", {})
                        )
                        name = (entity.get("legalName") or {}).get("name", "")
                        addr = entity.get("headquartersAddress") or {}
                        city, country = addr.get("city"), addr.get("country")
                        if not name or not city or country not in REGION:
                            continue
                        if (
                            fold(name) not in fold(ctx.get("content", ""))
                            or fold(city) not in fold(sentence)
                            or fold(city) not in fold(ctx.get("content", ""))
                        ):
                            continue
                        # The claim and its own returned context must support the city.
                        # The registry independently verifies the same HQ and country.
                        refs = [source(packet, ctx["content"], url)]
                        try:
                            ident = add_node(
                                "hq",
                                alias(name),
                                clean(city).title(),
                                country,
                                refs,
                                lei=lei,
                            )
                        except ValueError:
                            reject["registry and claim conflict"] += 1
                            continue
                        verified(
                            ident,
                            "https://search.gleif.org/#/record/" + lei,
                            reg.read_bytes(),
                            "gleif",
                        )

        if packet.get("endpoint") == "search":
            for ctx in packet["response"].get("context") or []:
                text = ctx.get("content", "")
                match = re.search(
                    r"^Entity: (.+?) \(type: Company, ID: [0-9a-f-]+\).*?headquarters_address = (.+?)(?:;|\. (?:Outgoing|Incoming)|\.$|$)",
                    text,
                    re.S,
                )
                if not match:
                    continue
                name = match[1]
                refs = context_refs(packet, [ctx["id"]])
                # Bare registry homepages are not citations for an individual company.
                refs = [
                    ref
                    for ref in refs
                    if urlsplit(ref[0]).path.strip("/")
                    or urlsplit(ref[0]).fragment.strip("/")
                ]
                place = hq_place(name, match[2], refs)
                if not place or not refs:
                    continue
                city, country = place
                try:
                    ident = add_node("hq", alias(name), clean(city), country, refs)
                except ValueError:
                    reject["conflicting or unsafe context"] += 1
                    continue
                registry_public(name, ident)
        if packet.get("endpoint") == "entity":
            entity = packet["response"]
            props = entity.get("properties") or {}
            prop = props.get("headquarters_address") or {}
            value = prop.get("value")
            if entity.get("entity_type") != "Company" or not isinstance(value, str):
                reject["entity without structured headquarters"] += 1
                continue
            refs = [
                source(packet, value, doc["document"])
                for doc in prop.get("sources", [])
                if isinstance(doc.get("document"), str)
                and doc["document"].startswith("https://")
            ]
            place = hq_place(entity["name"], value, refs)
            if not place or not refs:
                reject["entity address unresolved or uncited"] += 1
                continue
            city, country = place
            label = alias(entity["name"])
            try:
                ident = add_node("hq", label, clean(city), country, refs)
            except ValueError:
                reject["conflicting HQ"] += 1
                continue
            registry_public(entity["name"], ident)
            employee = props.get("employee_count") or {}
            count = employee.get("value")
            for doc in employee.get("sources") or []:
                url = doc.get("document")
                if (
                    type(count) is int
                    and count >= 0
                    and isinstance(url, str)
                    and url.startswith("https://")
                ):
                    nodes[ident]["size_band"] = (
                        "small" if count < 50 else "medium" if count < 250 else "large"
                    )
                    nodes[ident]["source_ids"].add(source(packet, str(count), url))
                    break
            lei = (props.get("lei") or {}).get("value")
            if (
                lei
                and re.fullmatch("[A-Z0-9]{18}[0-9]{2}", str(lei))
                and (props.get("lei") or {}).get("sources")
            ):
                # The public projection verifies and adds the LEI separately.
                doc = (props["lei"]["sources"][0] or {}).get("document")
                if isinstance(doc, str) and doc.startswith("https://"):
                    nodes[ident]["lei"] = lei
                    nodes[ident]["source_ids"].add(source(packet, lei, doc))
            continue
        for row in packet["response"].get("results") or []:
            url = row.get("source_url")
            if packet["question"].startswith(
                ("City centres", "Give city, country_code, geonames_id")
            ):
                url = next(
                    (
                        v
                        for k, v in row.items()
                        if "url" in k.lower()
                        and isinstance(v, str)
                        and "geonames.org/" in v
                    ),
                    None,
                )
            lei_hint = row.get("lei") or row.get("LEI")
            if (
                re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", str(lei_hint))
                and isinstance(url, str)
                and re.search(r"gleif|lei.lookup|lei.bloomberg", url, re.I)
            ):
                url = "https://search.gleif.org/#/record/" + lei_hint
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                reject["no row-level URL"] += 1
                continue
            try:
                if packet["question"].startswith(
                    ("City centres", "Give city, country_code, geonames_id")
                ):
                    cc = row.get("country_code")
                    city = clean(row.get("city"))
                    gid = str(row.get("geonames_id") or "")
                    if (
                        gid in geo
                        and gid in url
                        and urlsplit(url).hostname
                        in {"geonames.org", "www.geonames.org", "sws.geonames.org"}
                    ):
                        lat, lon = (
                            round(float(row["latitude"]), 1),
                            round(float(row["longitude"]), 1),
                        )
                        if gid in places.get((cc, fold(city)), set()) and geo[gid][
                            :3
                        ] == (cc, lat, lon):
                            refs = [
                                source(packet, json.dumps(row, sort_keys=True), url)
                            ]
                            coordinate[cc, key(city)] = (lat, lon, refs)
                    continue
                if "node_type" in row and row.get("name") and row.get("city"):
                    name, city, cc = (
                        clean(row["name"]),
                        clean(row["city"]),
                        row.get("country_code"),
                    )
                    desc = clean(row["node_type"]).lower()
                    kind = (
                        "inland_port"
                        if "inland port" in desc
                        else "port"
                        if ("port" in desc or "port" in name.lower())
                        and "airport" not in desc
                        else None
                    )
                    if not kind or cc not in REGION:
                        continue
                    basic = fold(
                        re.sub(
                            r"\bport\s+of\s+|\bseaport\b|\bport\b", "", name, flags=re.I
                        ).strip()
                    )
                    special = {
                        "jebelaliportdubai": ("AEJEA", "Jebel Ali"),
                        "portoftanjungpelepas": ("MYTPP", "Tanjung Pelepas"),
                        "laemchabangport": ("THLCH", "Laem Chabang"),
                        "portklang": ("MYPKG", "Port Klang"),
                        "portofalgecirasbay": ("ESALG", "Algeciras"),
                        "portofantwerpbruges": ("BEANR", "Antwerp"),
                    }
                    fix = special.get(fold(name))
                    if not fix and basic != fold(city):
                        reject["port identity not a city match"] += 1
                        continue
                    matches = [
                        (code, f)
                        for code, f in ports.items()
                        if f["Country"] == cc
                        and f["Function"].startswith("1")
                        and fold(city) in {fold(f["Name"]), fold(f["NameWoDiacritics"])}
                    ]
                    if fix and fix[0] in ports and ports[fix[0]]["Country"] == cc:
                        matches = [(fix[0], ports[fix[0]])]
                    if len(matches) != 1:
                        continue
                    code, f = matches[0]
                    label = fix[1] if fix else city if kind == "port" else name
                    canonical = named.get(label)
                    if canonical is None:
                        refs = [source(packet, json.dumps(row, sort_keys=True), url)]
                        canonical = add_node(
                            kind, label, city, cc, refs, industry="general_freight"
                        )
                        named[label] = canonical
                    raw = json.dumps(f, sort_keys=True).encode()
                    verified(
                        canonical,
                        "https://service.unece.org/trade/locode/" + cc.lower() + ".htm",
                        raw,
                        "unlocode",
                    )
                    try:
                        point = (
                            round(float(row["city_lat"]), 1),
                            round(float(row["city_lon"]), 1),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    found = unique_city(places, geo, cc, city, point)
                    if found:
                        gid, (_, lat, lon, digest) = found
                        refs = [source(packet, json.dumps(row, sort_keys=True), url)]
                        coordinate[cc, key(city)] = (lat, lon, refs)
                    continue
                company, city, country = (
                    row.get("company"),
                    row.get("headquarters_city"),
                    row.get("country_code"),
                )
                match = re.search(
                    r"(?<![A-Z0-9])([A-Z0-9]{18}[0-9]{2})(?![A-Z0-9])", url
                )
                if not company or not city or not match:
                    reject["requires source review"] += 1
                    continue
                lei = match.group(1)
                regpath = CACHE.parent / "registry" / (lei + ".json")
                if not regpath.is_file():
                    reject["registry unavailable"] += 1
                    continue
                record = (
                    read(regpath)
                    .get("data", {})
                    .get("attributes", {})
                    .get("entity", {})
                )
                hq = record.get("headquartersAddress", {})
                names = [record.get("legalName", {}), *(record.get("otherNames") or [])]
                if (
                    country != hq.get("country")
                    or key(city) != key(hq.get("city", ""))
                    or not any(
                        norm(company) == norm(item.get("name", "")) for item in names
                    )
                ):
                    reject["registry identity or location mismatch"] += 1
                    continue
                refs = [
                    source(
                        packet, json.dumps(row, sort_keys=True, ensure_ascii=False), url
                    )
                ]
                detail = clean(row["industry"]) if row.get("industry") else None
                sector = next(
                    (
                        s
                        for s, rx in [
                            ("pharma", "pharmaceut"),
                            ("automotive", "automot"),
                            ("electronics", "electronic|semiconductor"),
                            ("apparel", "apparel|clothing"),
                            ("chemicals", "chemical"),
                            ("food", "food|beverage"),
                            ("general_freight", "logistics|freight"),
                        ]
                        if detail and re.search(rx, detail, re.I)
                    ),
                    None,
                )
                ident = add_node(
                    "hq",
                    alias(company),
                    clean(city),
                    country,
                    refs,
                    lei=lei,
                    industry=sector,
                    industry_detail=None,
                    confidence="high",
                )
                verified(
                    ident,
                    "https://search.gleif.org/#/record/" + lei,
                    regpath.read_bytes(),
                    "gleif",
                )
            except (ValueError, TypeError, KeyError):
                reject["invalid structured scalar"] += 1
    for item in reviews:
        if item["kind"] == "hq":
            registry_public(item["name"], named[item["name"]])

    for r in read(archive / "network_review.json")["relationships"]:
        if r["id"] == "link_038":
            packet = read(archive / "network_evidence/lead.json")
            refs = context_refs(packet, [r["context_id"]])
            add_edge(
                "route",
                named["Hai Phong"],
                named["Los Angeles"],
                refs,
                "sea",
                "2026-04/2026-10",
                r["range_of_carrier_values_days"],
            )
    vsx = read(archive / "expansion/29.json")
    refs = context_refs(vsx, vsx["response"]["explainability"][0]["references"])
    for city, cc in [("Singapore", "SG"), ("Qinzhou", "CN")]:
        named[city] = add_node("port", city, city, cc, refs, industry="general_freight")
    leg = ["Hai Phong", "Cai Mep", "Singapore", "Qinzhou", "Hai Phong"]
    second = (CACHE.parent / "public/sg.html").read_bytes()
    service_ok = "Haiphong - Cai Mep - Singapore - Qinzhou - Haiphong" in second.decode(
        "utf8"
    ).replace("–", "-")
    for a, b in zip(leg, leg[1:]):
        add_edge("route", named[a], named[b], refs, "sea", "2025-08")
    codes = {
        "Singapore": "SGSIN",
        "Qinzhou": "CNQZH",
        "Hai Phong": "VNHPH",
        "Shekou": "CNSHK",
        "Cai Mep": "VNCMT",
        "Xiamen": "CNXAM",
        "Taipei": "TWTPE",
        "Los Angeles": "USLAX",
        "Oakland": "USOAK",
        "Rotterdam": "NLRTM",
        "Hamburg": "DEHAM",
        "Tallinn": "EETLL",
        "Valencia": "ESVLC",
    }
    for label, code in codes.items():
        entry = ports[code]
        if entry["Function"].startswith("1") and label in named:
            verified(
                named[label],
                "https://service.unece.org/trade/locode/" + code[:2].lower() + ".htm",
                json.dumps(entry, sort_keys=True).encode(),
                "unlocode",
            )
    rotation = (CACHE.parent / "public/rotation.html").read_bytes()
    rotation_text = rotation.decode("utf8").replace("–", "-")
    sequence = "Haiphong - Cai Mep - Shekou - Xiamen - Taipei - Los Angeles - Oakland - Shekou - Haiphong"
    if sequence in rotation_text and "February 2025" in rotation_text:
        for eid, edge in links.items():
            if (
                edge["mode"] == "sea"
                and edge["origin"] in public_evidence
                and edge["destination"] in public_evidence
            ):
                for url, _, _ in edge["source_ids"]:
                    if url.endswith("ps7-formerly-ap1-service-enhancement"):
                        verified(eid, url, rotation, "public_disclosure")

    if service_ok:
        for eid, edge in links.items():
            if edge["observed"] == "2025-08":
                for url, _, _ in edge["source_ids"]:
                    if url.endswith(
                        "service-enhancement-vietnam-singapore-express-vsx"
                    ):
                        verified(eid, url, second, "public_disclosure")
    for record in nodes.values():
        place = (record["country"], key(record["city"] or ""))
        if place in coordinate:
            record["lat"], record["lon"], refs = coordinate[place]
            record["source_ids"].update(refs)
            found = unique_city(
                places,
                geo,
                record["country"],
                record["city"],
                (record["lat"], record["lon"]),
            )
            if found:
                gid, item = found
                public_points[record["id"]] = (
                    item[1],
                    item[2],
                    "https://www.geonames.org/" + gid + "/",
                    item[3],
                )
    # A spelling or legal suffix can change without creating a second company.
    by_lei = {}
    for n in list(nodes.values()):
        if n["node_type"] == "hq" and n["lei"]:
            by_lei.setdefault(n["lei"], []).append(n)
    for same in by_lei.values():
        same.sort(key=lambda n: (n["alias"] not in old_alias.values(), n["alias"]))
        keep = same[0]
        for other in same[1:]:
            if keep["country"] != other["country"] or fold(keep["city"]) != fold(
                other["city"]
            ):
                raise ValueError("Conflicting headquarters for one LEI")
            keep["source_ids"].update(other["source_ids"])
            for field in ("industry", "size_band", "lat", "lon"):
                if keep[field] is None:
                    keep[field] = other[field]
            if other["id"] in public_evidence:
                public_evidence.setdefault(keep["id"], set()).update(
                    public_evidence[other["id"]]
                )
            if other["id"] in public_points:
                public_points[keep["id"]] = public_points[other["id"]]
            for a in work_alias:
                if work_alias[a] == other["alias"]:
                    work_alias[a] = keep["alias"]
            for node in nodes.values():
                for field in ("operator_alias", "owner_alias"):
                    if node[field] == other["alias"]:
                        node[field] = keep["alias"]
            del nodes[other["id"]]
    hq_ids = {n["alias"]: n["id"] for n in nodes.values() if n["node_type"] == "hq"}
    # Verify government registration and activity rows attribute by attribute.
    # An entity registry can confirm an address without confirming an economic sector.
    from grid_activities import enrich

    enrich(nodes, CACHE, source, verified, public_meta, reject)
    for site in nodes.values():
        owner = hq_ids.get(site["owner_alias"])
        if owner:
            add_edge("owns", owner, site["id"], site["source_ids"])
            edge_id = (
                "edge_" + sha(encode(["owns", owner, site["id"], None, None]))[:16]
            )
            if owner in public_evidence and site["id"] in public_evidence:
                public_evidence[edge_id] = public_evidence[site["id"]].copy()
    for item in read(CACHE / "reviewed_metrics.json"):
        n = next((n for n in nodes.values() if n["alias"] == item["alias"]), None)
        if n is None:
            continue
        refs = context_refs(read(CACHE / item["packet"]), item["contexts"])
        if not refs:
            continue
        values = {
            "throughput_band": clean(item["band"]),
            "throughput_year": item["year"],
        }
        n.update(values)
        n["source_ids"].update(refs)
        if item.get("doc"):
            verified(
                n["id"],
                item["url"],
                (CACHE.parent / "public" / item["doc"]).read_bytes(),
                "public_disclosure",
            )
            public_meta.setdefault(n["id"], {}).update(values)
    # Reuse independently published industry for the same prior supplier alias, but
    # only for a company whose retained Cala record also identifies that industry.
    for n in nodes.values():
        if n["node_type"] == "hq" and n["id"] in public_evidence and n["industry"]:
            matches = [
                f
                for f in prior
                if f["kind"] == "supplier"
                and f["subject_alias"] == n["alias"]
                and f["industry"] == n["industry"]
            ]
            if matches:
                old = origins[matches[0]["source_ids"][0]]
                verified(n["id"], old["url"], old["text_sha256"], "public_disclosure")
                public_meta.setdefault(n["id"], {})["industry"] = n["industry"]
    # Reviewed links use the exact Cala context and a field-matched operator publication.
    for selection in read(CACHE / "reviewed_links.json"):

        def endpoint(value):
            return next(
                (n["id"] for n in nodes.values() if n["alias"] == value),
                hq_ids.get(alias(value)),
            )

        a, b = endpoint(selection["origin"]), endpoint(selection["destination"])
        if not (a and b):
            continue
        packet = read(
            (archive / "network_evidence" if selection.get("legacy") else CACHE)
            / selection["packet"]
        )
        refs = context_refs(packet, selection["contexts"])
        if not refs:
            continue
        eid = add_edge(
            selection["kind"],
            a,
            b,
            refs,
            mode=selection.get("mode"),
            observed=selection.get("observed"),
            transit=selection.get("transit"),
        )
        if selection.get("doc"):
            raw = (CACHE.parent / "public" / selection["doc"]).read_bytes()
            verified(eid, selection["url"], raw, "public_disclosure")
            if selection.get("manufacturing_site"):
                site = nodes.get(selection["manufacturing_site"])
                if (
                    site
                    and site["node_type"] == "factory"
                    and site["operator_alias"] == nodes[a]["alias"]
                ):
                    verified(site["id"], selection["url"], raw, "public_disclosure")
                    public_meta.setdefault(site["id"], {})["products"] = site[
                        "products"
                    ]
    if public:
        nodes = {k: v for k, v in nodes.items() if k in public_evidence}
        links = {
            k: v
            for k, v in links.items()
            if k in public_evidence
            and v["origin"] in nodes
            and v["destination"] in nodes
        }
        for r in [*nodes.values(), *links.values()]:
            refset = public_evidence[r["id"]].copy()
            if "node_type" in r:
                r["industry"] = None if r["node_type"] == "hq" else r["industry"]
                r["products"] = None
                r["size_band"] = None
                r["throughput_band"] = None
                r["throughput_year"] = None
                r.update(public_meta.get(r["id"], {}))
                if r["id"] in public_points:
                    lat, lon, url, digest = public_points[r["id"]]
                    r["lat"], r["lon"] = lat, lon
                    refset.add((url, digest, "geonames"))
                else:
                    r["lat"] = r["lon"] = None
            keys = set()
            for url, digest, provider in refset:
                qid = "q_public_" + sha(url.encode())[:12]
                k = (url, digest, qid)
                logs[qid] = dict(
                    id=qid,
                    endpoint="Public reference record",
                    params={},
                    n_results=1,
                    retrieved_at="2026-11-14",
                )
                sources[k] = dict(
                    provider=provider,
                    url=url,
                    publisher=urlsplit(url).hostname,
                    published_at=None,
                    query_id=qid,
                    retrieved_at="2026-11-14",
                    title="Public source record",
                    text_sha256=digest,
                )
                keys.add(k)
            r["source_ids"] = keys

    if not public:
        company_ids = {}
        for n in nodes.values():
            if n["node_type"] == "hq":
                company_ids[n["alias"]] = n["id"]
        for r in read(archive / "network_review.json")["relationships"]:
            if not r.get("cala_query") or r["relation"] != "supplies":
                continue
            a = alias(r.get("subject_inventory_name") or r["subject"])
            b = alias(r.get("object_inventory_name") or r["object"])
            if a in company_ids and b in company_ids:
                packet = read(
                    archive / "network_evidence" / (r["cala_query"] + ".json")
                )
                refs = context_refs(packet, [r["context_id"]])
                add_edge("supplies", company_ids[a], company_ids[b], refs)
    used = set().union(*(r["source_ids"] for r in [*nodes.values(), *links.values()]))
    ids = {s: f"src_cala_{n:06}" for n, s in enumerate(sorted(used), 600000)}
    for row in [*nodes.values(), *links.values()]:
        row["source_ids"] = sorted(ids[s] for s in row["source_ids"])
    out.mkdir(parents=True, exist_ok=True)
    rows = sorted(nodes.values(), key=lambda x: x["id"])
    edges = sorted(links.values(), key=lambda x: x["id"])
    docs = [dict(id=ids[s], **sources[s]) for s in sorted(used)]

    def put(name, data):
        (out / name).write_bytes(encode(data))

    put("grid_nodes.json", rows)
    put("grid_edges.json", edges)
    put("sources.json", docs)
    put(
        "sanitise_report.json",
        dict(reasons=dict(reject), unit="structured rows excluded"),
    )
    hqs = [r for r in rows if r["node_type"] == "hq"]
    gaps = [r["id"] for r in rows if r["lat"] is None]
    eu = sum(r["region"].startswith("europe_") for r in hqs)
    lines = [
        "# Cala starting grid",
        "",
        f"European headquarters: {eu}/{len(hqs)} ({eu / len(hqs):.1%}).",
        f"European nodes: {sum(r['region'].startswith('europe_') for r in rows)}/{len(rows)}.",
        f"Mapped at city level: {len(rows) - len(gaps)}/{len(rows)}.",
        "",
    ]
    lines += [
        f"Rest-of-world headquarters: {len(hqs) - eu}/{len(hqs)} ({(len(hqs) - eu) / len(hqs):.1%}).",
        "",
    ]
    for field, group in [
        ("country", rows),
        ("node_type", rows),
        ("industry", rows),
        ("edge_type", edges),
    ]:
        counts = Counter(r[field] or "unknown" for r in group)
        if field == "node_type":
            for item in "hq factory warehouse distribution_centre supply_centre port airport_hub rail_terminal inland_port".split():
                counts.setdefault(item, 0)
        elif field == "edge_type":
            for item in ("supplies", "owns", "ships_via", "route"):
                counts.setdefault(item, 0)
        lines += [f"| {field} | Count |", "| --- | ---: |"]
        lines += [f"| {value} | {count} |" for value, count in sorted(counts.items())]
        lines += [""]
    lines += [
        "## Gaps",
        "",
        "The target is 400 companies. Uncited candidates are excluded. An operator does not establish ownership. Missing industry, throughput, capacity, status, risk and transport fields are unknown. Routes are dated observations where the evidence gives a date. No supplier allocation or dependence is inferred.",
        "",
        "An unknown industry is common for public HQ records because an LEI entry verifies the legal entity and address without establishing its business activity. Coordinates use cited city observations that match GeoNames to one decimal. The coverage target is measured by headquarters, independently of the number of sites or routes.",
        "",
        "City coordinates missing for:",
        "",
        *["- " + g for g in gaps],
    ]
    (out / "coverage.md").write_bytes(("\n".join(lines) + "\n").encode())
    lookup = {s["id"]: s for s in docs}
    checks = random.Random(1729).sample(rows + edges, min(20, len(rows) + len(edges)))

    def describe(r):
        if "node_type" in r:
            return f"{r['alias']} | {r['node_type']} | {r['city'] or 'unknown city'}, {r['country']}"
        return f"{r['edge_type']} | {nodes[r['origin']]['alias']} to {nodes[r['destination']]['alias']} | {r['mode'] or ''} {r['observed'] or ''}"

    checks_text = "\n".join(
        r["id"]
        + "  "
        + describe(r)
        + " | "
        + ", ".join(lookup[s]["url"] for s in r["source_ids"])
        for r in checks
    )
    (out / "spot_check.txt").write_bytes((checks_text + "\n").encode())
    markers = [[r["lat"], r["lon"], r["alias"]] for r in rows if r["lat"] is not None]
    template = (Path(__file__).with_name("starting_grid_map.html")).read_text(
        encoding="utf-8"
    )
    (out / "map.html").write_bytes(
        template.replace(
            "GRID_MARKERS", json.dumps(markers).replace("<", "\\u003c")
        ).encode()
    )
    files = {
        p.name: sha(p.read_bytes())
        for p in sorted(out.iterdir())
        if p.is_file() and p.name != "manifest.json"
    }
    queryids = {s["query_id"] for s in docs}
    put(
        "manifest.json",
        dict(
            contract_version="0.2",
            fragment="cala/starting_grid_v0",
            provider="public_disclosure" if public else "cala",
            built_at=max(logs[q]["retrieved_at"] for q in queryids),
            built_by="SupplyGym",
            query_log=[logs[q] for q in sorted(queryids)],
            files=files,
            counts=dict(grid_nodes=len(rows), grid_edges=len(edges), sources=len(docs)),
            licence_note="Public-source factual abstractions. GeoNames CC BY 4.0. https://www.geonames.org/ | GLEIF CC0 | UN/LOCODE and named disclosures."
            if public
            else "Private Cala research snapshot. Source documents retain their rights. https://www.cala.ai/terms",
            pack_hash=sha(
                "\n".join(k + ":" + v for k, v in sorted(files.items())).encode()
            ),
        ),
    )
    (CACHE / "grid_alias_map.json").write_bytes(encode(work_alias))
    print(checks_text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    build(
        args.archive,
        args.out or (OUTPUT if args.public else CACHE.parent / "starting_grid_v0"),
        args.public,
    )
