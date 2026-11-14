"""Cross-check structured Cala activity rows against national registry evidence."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


class RegistryFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []
        self.active = False

    def handle_starttag(self, tag, attrs):
        target = dict(attrs).get("id", "")
        if target.startswith("sic") or target == "company-type":
            self.active = True

    def handle_endtag(self, tag):
        self.active = False

    def handle_data(self, data):
        if self.active and data.strip():
            self.values.append(data.strip())


def enrich(nodes, cache, source, verified, public_meta, reject):
    companies = {}
    for n in nodes.values():
        if not n.get("lei") or n["country"] not in {"GB", "FR"}:
            continue
        path = cache.parent / "registry" / (n["lei"] + ".json")
        if not path.exists():
            continue
        entity = (
            json.loads(path.read_bytes())
            .get("data", {})
            .get("attributes", {})
            .get("entity", {})
        )
        num = str(entity.get("registeredAs") or "").replace(" ", "")
        if re.fullmatch(r"[A-Z0-9]{8,9}", num):
            companies[n["country"], num] = n
    for f in sorted(cache.glob("?" * 64 + ".json")):
        p = json.loads(f.read_bytes())
        if "activity_code (SIC or NAF)" not in p.get("question", ""):
            continue
        for row in p["response"].get("results") or []:
            url = row.get("source_url")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            host = urlsplit(url).hostname
            num = str(row.get("registry_number") or "").replace(" ", "").strip()
            country = (
                "GB"
                if host == "find-and-update.company-information.service.gov.uk"
                else "FR"
                if host
                in {
                    "annuaire-entreprises.data.gouv.fr",
                    "data.inpi.fr",
                    "recherche-entreprises.api.gouv.fr",
                }
                else None
            )
            n = companies.get((country, num))
            if not n or num not in url:
                continue
            dst = (
                cache.parent
                / "national"
                / (n["lei"] + (".html" if country == "GB" else ".json"))
            )
            if not dst.is_file():
                continue
            raw = dst.read_bytes()
            code = str(row.get("activity_code") or "").strip()
            if country == "GB":
                reg = RegistryFields()
                reg.feed(raw.decode("utf8"))
                candidates = re.findall(r"(?<!\d)\d{5}(?!\d)", code)
                matches = [
                    c
                    for c in candidates
                    if any(v.startswith(c + " ") for v in reg.values)
                ]
                code = next(
                    (
                        c
                        for c in matches
                        if c[:2]
                        in {
                            "10",
                            "11",
                            "14",
                            "20",
                            "21",
                            "26",
                            "29",
                            "52",
                            "22",
                            "24",
                            "25",
                            "28",
                        }
                    ),
                    next(iter(matches), ""),
                )
                if num not in raw.decode("utf8"):
                    continue
                public_url = (
                    "https://find-and-update.company-information.service.gov.uk/company/"
                    + num
                )
                forms = reg.values
            else:
                matches = [
                    r for r in json.loads(raw).get("results", []) if r["siren"] == num
                ]
                if len(matches) != 1 or str(
                    matches[0].get("activite_principale", "")
                ).replace(".", "") != code.replace(".", ""):
                    continue
                public_url = (
                    "https://recherche-entreprises.api.gouv.fr/search?q="
                    + num
                    + "&per_page=1"
                )
                forms = []
            # SIC 2007 and NAF Rev.2 use these shared NACE divisions.
            sector = {
                "10": "food",
                "11": "food",
                "14": "apparel",
                "20": "chemicals",
                "21": "pharma",
                "26": "electronics",
                "29": "automotive",
                "52": "general_freight",
            }.get(code[:2])
            detail = {
                "22": "plastics and rubber",
                "24": "basic metals",
                "25": "fabricated metals",
                "28": "machinery",
            }.get(code[:2])
            data = {"registry_number": num}
            if sector:
                data["industry"] = sector
            if detail:
                data["industry_detail"] = detail
            form = str(row.get("company_type") or "").lower()
            for form_text, value in [
                ("public limited company", "public"),
                ("private limited company", "private"),
            ]:
                if form == form_text and any(v.lower() == form_text for v in forms):
                    data["public_private"] = value
            n.update(data)
            n["source_ids"].add(source(p, json.dumps(row, sort_keys=True), url))
            public_meta.setdefault(n["id"], {}).update(data)
            verified(n["id"], public_url, raw, "national_registry")
