# Cala-backed SupplyGym data

The Cala regime uses historical supplier relationships and operating-site
geography, with explicitly synthetic operating parameters. It is a replenishment
scenario, not a replay of historical procurement or a verified electronics bill
of materials. Episodes and grading run offline.

The original [Cala data package](https://github.com/vaartnou/crucible/pull/17),
revision `0bc537dc31d32e192aa06982be66e963b06e25e2`, contains 841 observations
from eight public disclosures: 218 supplier-to-buyer observations covering 217
supplier aliases, 607 sites in 24 countries, and 16 freight routes. Its manifest
is contract v0.1, provider `public_disclosure`, with pack hash
`c7c26757a917bd4f851ad117bba172c5404714045f88ae147331edd5269e1326`.
These facts were independently abstracted from disclosures; they are not raw
Cala API output. The original offline validator passes source references,
supplier/site joins, route attributes and file/pack hashes.

The runtime package,
`engine/src/crucible/envs/supplygym/data/cala_world_v0.json`, selects 168 original
facts and retains their source metadata: 12 supplier relationships with
`Buyer-0001` and 156 site observations. There are two sources: the February 2018
supplier disclosure about 2017 (166 facts) and the August 2024 power-semiconductor
facility announcement (two facts). The package also carries 168 node-shaped
`world_facts` projections for the shared v0.2 contract; original records remain
available under `facts` for audit.

Ten selected aliases serve as scenario incumbents: `Supplier-0001`,
`Supplier-0040`, `Supplier-0042`, `Supplier-0043`, `Supplier-0078`,
`Supplier-0089`, `Supplier-0092`, `Supplier-0106`, `Supplier-0119`, and
`Supplier-0131`. `Supplier-0149` and `Supplier-0056` serve as backups. Those
roles and their ability to supply one interchangeable unit are model assumptions.

| Fields or behavior | Mapping and limitation |
| --- | --- |
| Identity and provenance | Original fact IDs, aliases and `source_ids` remain traceable. Projection confidence is a review label, not a Cala confidence score. |
| `supplies` | Original supplier aliases point to `Buyer-0001`. Site-to-operator output edges are marked `synthetic_fill`; ownership does not establish an upstream component contract. |
| `region` / `sim_region` | Site countries determine geographic exposure. North America maps to NA; Latin America to LATAM; Europe and the Middle East to EU; East Asia to APAC-N; South and Southeast Asia to APAC-S. Supplier `global` regions have no headquarters assignment; projected regions use the largest site count, while runtime display regions use seeded site weights. |
| `node_kind` / `tier` | Ten direct and two alternative suppliers are public; 156 site exposure nodes are hidden. Scenario roles and internal site tiers are flagged synthetic. There is no sourced tier-3 commercial graph or guarantee of two direct suppliers per region. |
| `lat`, `lon`, `port`, `lead_days`, `price_index`, `capacity_band` | Unsupported source values remain null in the projection. Runtime map positions, port exposure, lead, price and capacity are separately marked synthetic. |
| Price and capacity | One seeded incumbent quote of $10.10–$10.20 per unit; backups share a seeded $1.80–$2.20 premium. Incumbent capacities are 31–33 units/day, backups 33–35. These are benchmark parameters, not quotes or factory capacities. |
| Freight and opening state | Shared seeded lead of 17–20 days, with supplier-specific receipt slots in a four-day regular freight cycle. The opening book contains prepaid regular orders allocated across incumbents, with inventory position at the default reorder point. No source establishes these services or opening orders. |
| Disruptions and propagation | Seeded site-output weights determine partial geographic losses. Corporate fire/bankruptcy and inherited synthetic event rates/durations remain scenario mechanics. Several disclosed addresses may share a campus; they do not imply independent full capacities. |
| News, rumors and event rates | This slice contains no empirical event templates, signal fragment or `rates.json`. Wording, timing, noise and rates remain generated. Future v0.2 signal integration requires sanitized `{w}` text and matching template/seed splits. |
| Integrity and agent views | Source revision, original pack hash, exact package digest and canonical assumptions digest bind world identity. Agents see aliases and operational observations; source titles, URLs, hidden site metadata and future outcomes stay outside their tools. |

The selected sites cover 20 countries and all five simulation regions: APAC-N
93, APAC-S 30, NA 16, EU 14 and LATAM 3. Their fine-region counts are China 58,
other East Asia 35, Southeast Asia 29, North America 16, Western Europe 9,
Latin America 3, Middle East 3, Eastern Europe 2 and South Asia 1. Geographic
coverage is concentrated rather than balanced; APAC-N accounts for about 60%
of selected sites. The full source pack is also concentrated, with China
containing 279 of its 607 sites. Geographic breadth does not establish the
v0.2 target of balanced commercial tiers.

The full source pack's unused freight facts describe 14 February 2024 ocean
service/destination combinations from Hai Phong and two April 2019 air services
via Budapest. Ocean ranges are published transit times, not production or
end-to-end fulfillment times; air facts supply announced weekly frequencies.
No fact connects a selected supplier to one of these routes. The full pack also
contains supplier-award and manufacturing-arrangement observations; they supply
neither procurement shares nor interchangeable capacity and are outside this
selected runtime graph.

The shared v0.2 contract requires node-shaped facts, explicit synthetic fills,
provenance, offline execution and hashes bound to scoring. This package preserves
the immutable v0.1 source facts alongside its v0.2 projection. Its company/site
model differs from the contract's target of 10 direct, two alternative, 12
commercial tier-2 and eight tier-3 nodes. The projection records that difference
instead of presenting site ownership as a verified three-tier supply chain.

The source licence note covers independent factual abstractions and retains
the original documents' rights. Raw provider responses, source-document bodies
and the real-name alias map are excluded from the published runtime package.
The [Cala terms link](https://www.cala.ai/terms) does not itself establish a
redistribution grant for raw provider data. Attribution and the distinction
between historical observations and synthetic parameters remain part of reuse.
