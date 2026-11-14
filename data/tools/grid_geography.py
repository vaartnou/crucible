"""Resolve already cited places against public city and transport reference files."""

import csv
import hashlib
from zipfile import ZipFile
from collections import defaultdict
import unicodedata


def fold(text):
    text = text.translate(
        str.maketrans(
            {
                "Ł": "L",
                "ł": "l",
                "Ø": "O",
                "ø": "o",
                "Đ": "D",
                "đ": "d",
                "Æ": "AE",
                "æ": "ae",
                "Œ": "OE",
                "œ": "oe",
            }
        )
    )
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text.casefold())
        if ch.isascii() and ch.isalnum()
    )


def load_cities(path):
    places = defaultdict(set)
    info = {}
    with ZipFile(path) as archive, archive.open("cities1000.txt") as f:
        for raw in f:
            cols = raw.decode("utf-8").rstrip("\r\n").split("\t")
            if cols[6] != "P" or cols[7] in {"PPLX", "PPLH", "PPLQ", "PPLW"}:
                continue
            gid = cols[0]
            info[gid] = (
                cols[8],
                round(float(cols[4]), 1),
                round(float(cols[5]), 1),
                hashlib.sha256(raw).hexdigest(),
            )
            for name in {cols[1], cols[2]}:
                places[cols[8], fold(name)].add(gid)
    return places, info


def reference_files(directory):
    places, info = load_cities(directory / "cities1000.zip")
    with (directory / "ports.csv").open(encoding="utf-8", newline="") as f:
        ports = {r["Country"] + r["Location"]: r for r in csv.DictReader(f)}
    return places, info, ports


def unique_city(places, info, cc, city, cited_point=None):
    matches = places.get((cc, fold(city or "")), set())
    points = [(gid, info[gid]) for gid in matches]
    if cited_point:
        points = [item for item in points if tuple(item[1][1:3]) == tuple(cited_point)]
    if len(points) == 1:
        return points[0]
