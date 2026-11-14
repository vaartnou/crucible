"""Match specific, sourced headquarters addresses to cities in the public gazetteer."""

import re
from grid_geography import fold

COUNTRIES = {
    "finland": "FI",
    "germany": "DE",
    "italy": "IT",
    "spain": "ES",
    "france": "FR",
    "unitedkingdom": "GB",
    "england": "GB",
    "switzerland": "CH",
    "sweden": "SE",
    "netherlands": "NL",
    "belgium": "BE",
    "denmark": "DK",
    "poland": "PL",
    "unitedstates": "US",
    "unitedstatesofamerica": "US",
    "usa": "US",
    "japan": "JP",
    "austria": "AT",
    "norway": "NO",
    "brazil": "BR",
    "india": "IN",
    "czechrepublic": "CZ",
    "czechia": "CZ",
    "romania": "RO",
    "republicofkorea": "KR",
    "southkorea": "KR",
    "taiwan": "TW",
    "china": "CN",
    "turkey": "TR",
    "turkiye": "TR",
    "ireland": "IE",
}


def locality(value, places, countries):
    if (
        not isinstance(value, str)
        or len(value) > 750
        or "://" in value
        or re.search(r"ignore|instructions|system prompt|<|>", value, re.I)
    ):
        return None
    parts = [p.strip(" .") for p in value.strip().split(",")]
    if len(parts) < 3:
        return None
    country = parts[-1].upper()
    if country not in countries:
        country = COUNTRIES.get(fold(parts[-1]))
    if country not in countries:
        return None
    matches = {}
    for part in parts[1:-1]:
        part = re.sub(
            r"^(?:[A-Z]{1,3}-)?\d{4,6}\s+|\s+\d{4,6}(?:-\d+)?$", "", part
        ).strip()
        if (country, fold(part)) in places:
            matches[fold(part)] = part
    return (
        (next(iter(matches.values())).title(), country) if len(matches) == 1 else None
    )
