"""Cross-check Cala's row citations against GLEIF without sending credentials."""

from concurrent.futures import ThreadPoolExecutor
import json
import re
from urllib.error import HTTPError
from urllib.request import urlopen

from probe_cala import ROOT


def identifiers(cache):
    ids = set()
    for path in cache.glob("*.json"):
        packet = json.loads(path.read_bytes())
        if not isinstance(packet, dict):
            continue
        for prop in (packet.get("response", {}).get("properties") or {}).values():
            for item in prop.get("sources") or []:
                url = item.get("document")
                if isinstance(url, str) and re.fullmatch(
                    r"https://search.gleif.org/#/record/[A-Z0-9]{20}", url
                ):
                    ids.add(url.rsplit("/", 1)[1])
        for ctx in packet.get("response", {}).get("context") or []:
            for origin in ctx.get("origins") or []:
                url = (origin.get("document") or {}).get("url", "")
                if re.fullmatch(
                    r"https://(?:search\.gleif\.org/#/record|lei\.bloomberg\.com/leis/view|www\.lei-lookup\.com/record)/[A-Z0-9]{20}",
                    url,
                ):
                    ids.add(url.rsplit("/", 1)[1])
        for row in packet.get("response", {}).get("results") or []:
            url = str(row.get("source_url") or "")
            found = re.search(r"(?<![A-Z0-9])([A-Z0-9]{18}[0-9]{2})(?![A-Z0-9])", url)
            identifier = row.get("LEI") or row.get("lei")
            if re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", str(identifier)) and re.search(
                r"gleif|lei.lookup|lei.bloomberg", url, re.I
            ):
                ids.add(identifier)
            if found and any(
                host in url
                for host in [
                    "gleif.org/",
                    "lei-lookup.com/",
                    "bloomberg.com/",
                    "lei.info/",
                    "lei.bloomberg.com/",
                ]
            ):
                ids.add(found.group(1))
    return sorted(ids)


def verify(lei, directory):
    path = directory / (lei + ".json")
    if path.is_file():
        return
    try:
        with urlopen(
            "https://api.gleif.org/api/v1/lei-records/" + lei, timeout=22
        ) as res:
            packet = json.load(res)
    except HTTPError as exc:
        if exc.code != 404:
            return
        packet = {"not_found": True}
    except OSError:
        return
    path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    cache = ROOT / ".cala-private/grid"
    directory = ROOT / ".cala-private/registry"
    directory.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda lei: verify(lei, directory), identifiers(cache)))
    print("Cached registry checks:", len(list(directory.glob("*.json"))))
