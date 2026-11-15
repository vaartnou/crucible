"""Offline, source-grounded supplier geography with explicitly synthetic operations.

The public facts establish historical supplier/buyer links and operating countries.
They do not establish SKU compatibility, commercial terms, output shares, current
availability, or a shared regional distribution buffer. Those are scenario assumptions.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

import numpy as np

from crucible.envs.supplygym.world import REGION_XY, World

COUNTRY_REGION = {
    "US": "NA",
    "MX": "LATAM",
    "BR": "LATAM",
    "AT": "EU",
    "BE": "EU",
    "CZ": "EU",
    "DE": "EU",
    "IE": "EU",
    "IL": "EU",
    "CN": "APAC-N",
    "JP": "APAC-N",
    "KR": "APAC-N",
    "TW": "APAC-N",
    "ID": "APAC-S",
    "IN": "APAC-S",
    "MY": "APAC-S",
    "PH": "APAC-S",
    "SG": "APAC-S",
    "TH": "APAC-S",
    "VN": "APAC-S",
}

# Canonical JSON below is part of the world identity. Every number describes a
# benchmark scenario, never a claim about an actual supplier's operations.
ASSUMPTIONS: dict[str, Any] = {
    "schema": "supplygym-cala-assumptions-v1",
    "sku": "one synthetic interchangeable electronics replenishment unit",
    "roles": "first ten selected historical buyer suppliers are incumbents; final two are backups",
    "service": "regular freight consolidates at supplier-specific receipt slots",
    "receipt_cycle_days": 4,
    "receipt_slot_rng_stream": 914,
    "opening_stock": "prepaid regular orders, one mean demand per day allocated across incumbents, "
    "with inventory position at the default reorder point",
    "direct_price_uniform_usd": [10.1, 10.2],
    "alternative_price_premium_uniform_usd": [1.8, 2.2],
    "shared_lead_days_inclusive": [17, 20],
    "direct_capacity_inclusive_units_daily": [31, 33],
    "alternative_capacity_inclusive_units_daily": [33, 35],
    "terminal_value_usd": 10.0,
    "corporate_base_fail_uniform": [0.02, 0.04],
    "site_base_fail_uniform": [0.03, 0.08],
    "site_output_weight_uniform_then_normalize": [0.5, 1.5],
    "site_port_exposure_probability": 0.6,
    "map_jitter_uniform": [-7.0, 7.0],
    "country_region": COUNTRY_REGION,
    "region_projection": "MX maps to LATAM and IL maps to EU to retain the five-region interface",
    "corporate_region": "largest seeded operating-site exposure; display and PPO summary only",
    "regional_shock_target": "disclosed operating sites, not a second corporate headquarters hit",
    "site_loss_propagation": "weighted loss uses unchanged world.GAMMA_PROP=0.8",
    "snapshot": "2017 supplier disclosure plus 2024 site observations; historical sourcing",
    "site_interpretation": "disclosed addresses are exposure nodes; some may share a campus",
    "rng": "numpy PCG64 via SeedSequence([world_seed, 301]); sorted fact IDs",
    "parameter_provenance": "all scenario parameters here are fixed synthetic modeling choices",
    "inherited_event_rates_per_year": {
        "strike": 1.5,
        "port_closure": 0.7,
        "sanction": 0.2,
        "fire_base_fail_multiplier": 1.5,
        "bankruptcy": 0.04,
    },
    "inherited_event_duration_days": {
        "strike": [7, 21],
        "port_closure": [5, 15],
        "sanction": [45, 90],
        "fire": [30, 60],
        "bankruptcy": [150, 365],
    },
    "inherited_event_provenance": "fixed synthetic world.py defaults; no measured calibration",
}


def _slice_bytes() -> bytes:
    return files("crucible.envs.supplygym").joinpath("data/cala_world_v0.json").read_bytes()


def cala_world_identity() -> dict[str, str]:
    """Stable source/assumption fingerprints for task/scoring identity binding."""
    data = _slice_bytes()
    pack = json.loads(data)
    canonical = json.dumps(ASSUMPTIONS, sort_keys=True, separators=(",", ":")).encode()
    return {
        "kind": "cala",
        "schema": pack["schema"],
        "source_revision": pack["source_revision"],
        "source_pack_hash": pack["source_pack_hash"],
        "slice_sha256": hashlib.sha256(data).hexdigest(),
        "assumptions_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def make_cala_world(seed: int = 7) -> World:
    """Construct 10 incumbents + 2 backups and hidden, weighted operating sites.

    Public supplier regions are representative exposures. Regional disruptions
    affect each actual site country; a multi-region supplier loses only its
    weighted affected output. Company-specific fires/bankruptcies still operate.
    """
    pack = json.loads(_slice_bytes())
    rng = np.random.default_rng(np.random.SeedSequence([seed, 301]))
    aliases = pack["direct_aliases"] + pack["alternative_aliases"]
    direct_price = round(float(rng.uniform(*ASSUMPTIONS["direct_price_uniform_usd"])), 2)
    alt_price = round(
        direct_price + float(rng.uniform(*ASSUMPTIONS["alternative_price_premium_uniform_usd"])), 2
    )
    lead_lo, lead_hi = ASSUMPTIONS["shared_lead_days_inclusive"]
    shared_lead = int(rng.integers(lead_lo, lead_hi + 1))
    slots = np.random.default_rng(
        np.random.SeedSequence([seed, ASSUMPTIONS["receipt_slot_rng_stream"]])
    ).integers(ASSUMPTIONS["receipt_cycle_days"], size=len(aliases))
    nodes: list[dict[str, Any]] = []
    parents: dict[int, list[tuple[int, float]]] = {}
    sites_by_operator = {
        alias: sorted(
            (
                f
                for f in pack["facts"]
                if f["kind"] == "site" and f["attributes"]["operator"] == alias
            ),
            key=lambda f: f["id"],
        )
        for alias in aliases
    }
    # Public ordering is the fixed 17-action PPO contract. No hidden nodes may
    # precede these twelve supplier nodes.
    for i, alias in enumerate(aliases):
        alternative = i >= 10
        source_fact = next(
            fact
            for fact in pack["facts"]
            if fact["kind"] == "supplier" and fact["subject_alias"] == alias
        )
        cap_lo, cap_hi = ASSUMPTIONS[
            "alternative_capacity_inclusive_units_daily"
            if alternative
            else "direct_capacity_inclusive_units_daily"
        ]
        nodes.append(
            {
                "id": f"A{i - 9:02d}" if alternative else f"S{i + 1:02d}",
                "name": alias,
                "tier": 1,
                "kind": "alt" if alternative else "direct",
                "region": "NA",
                "x": 0.0,
                "y": 0.0,
                "cap": int(rng.integers(cap_lo, cap_hi + 1)),
                "lead": shared_lead,
                "receipt_slot": int(slots[i]),
                "price": alt_price if alternative else direct_price,
                "base_fail": round(
                    float(rng.uniform(*ASSUMPTIONS["corporate_base_fail_uniform"])), 3
                ),
                "port": False,
                "regional_exposure": False,
                "fact_id": source_fact["id"],
                "source_ids": source_fact["source_ids"],
                "supplies": source_fact["attributes"]["supplies"],
                "synthetic_fill": {
                    "id": "fixed scenario slot",
                    "tier": "fixed scenario role",
                    "kind": "fixed scenario role",
                    "regional_exposure": "fixed modeling choice",
                    "region": "derived from seeded site weights",
                    "x": "synthetic region map",
                    "y": "synthetic region map",
                    "cap": "seeded integer draw",
                    "lead": "shared seeded contract draw",
                    "receipt_slot": "seeded offset in a synthetic scheduled freight service",
                    "price": "shared seeded contract draw",
                    "base_fail": "seeded draw",
                    "port": "fixed corporate exclusion",
                },
            }
        )
    for i, alias in enumerate(aliases):
        facts = sites_by_operator[alias]
        if not facts:
            raise ValueError(f"No disclosed operating sites for {alias}")
        raw_weights = rng.uniform(
            *ASSUMPTIONS["site_output_weight_uniform_then_normalize"], size=len(facts)
        )
        weights = raw_weights / raw_weights.sum()
        exposure: dict[str, float] = {}
        parents[i] = []
        for fact, weight in zip(facts, weights, strict=True):
            region = COUNTRY_REGION[fact["attributes"]["country"]]
            exposure[region] = exposure.get(region, 0.0) + float(weight)
            x0, y0 = REGION_XY[region]
            site_index = len(nodes)
            nodes.append(
                {
                    "id": f"SITE-{site_index:03d}",
                    # News templates can interpolate name: keep site identity hidden.
                    "name": f"{region} operations",
                    "tier": 2,
                    "kind": "site",
                    "region": region,
                    "x": round(float(x0 + rng.uniform(-7, 7)), 1),
                    "y": round(float(y0 + rng.uniform(-7, 7)), 1),
                    "cap": 0,
                    "lead": 0,
                    "price": 0,
                    "base_fail": round(
                        float(rng.uniform(*ASSUMPTIONS["site_base_fail_uniform"])), 3
                    ),
                    "port": bool(rng.random() < ASSUMPTIONS["site_port_exposure_probability"]),
                    "fact_id": fact["id"],
                    "subject_alias": fact["subject_alias"],
                    "country": fact["attributes"]["country"],
                    "source_ids": fact["source_ids"],
                    "regional_exposure": True,
                    "synthetic_fill": {
                        "id": "fixed scenario slot",
                        "name": "fixed anonymized region label",
                        "tier": "fixed scenario site tier",
                        "kind": "fixed scenario site role",
                        "x": "seeded map jitter",
                        "y": "seeded map jitter",
                        "cap": "fixed unused hidden-node placeholder",
                        "lead": "fixed unused hidden-node placeholder",
                        "price": "fixed unused hidden-node placeholder",
                        "base_fail": "seeded draw",
                        "port": "seeded Bernoulli draw",
                        "regional_exposure": "fixed modeling choice",
                        "output_weight": "normalized seeded draw; no observed procurement shares",
                    },
                }
            )
            parents[i].append((site_index, float(weight)))
        representative = max(exposure, key=lambda region: exposure[region])
        x0, y0 = REGION_XY[representative]
        nodes[i].update(region=representative, x=x0, y=y0)
    return World(int(seed), nodes, parents)
