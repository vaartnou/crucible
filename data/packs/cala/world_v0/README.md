# Cala data package for SupplyGym

This frozen world fragment contains 841 historical observations from eight primary disclosures. Read `world_facts.json` during world construction and use `source_ids` to look up each record in `sources.json`. The episode runner can use the snapshot offline.

| Records | Count | Use in the simulation |
| --- | ---: | --- |
| Supplier-to-buyer observations | 218 | Construct a directed network with 217 supplier aliases. A supplier can serve more than one buyer. |
| Site observations | 607 | Join `attributes.operator` to a supplier alias to model geographic exposure across 24 countries. |
| Freight routes | 16 | Compare 14 ocean service and destination combinations with published transit times and two announced air services via Budapest. |

## Reading the graph

Each supplier record points from its `subject_alias` to the aliases in `attributes.supplies`. `Buyer-0001` is a downstream buyer that appears only as a destination. A supplier with region `global` has no headquarters assignment in this snapshot. An award identifies a supplier relationship in the listed years. The record carries no procurement volume or share.

Most site observations come from a partial extraction of the February 2018 supplier disclosure covering 2017. Each alias identifies a disclosed supplier address. Different addresses may belong to one campus. Headquarters-only entries contributed supplier relationships. The 2024 site records identify power semiconductor operations. The older disclosure is available as a [PDF copy](https://manuals.plus/m/9b958fa7982079df737e27beae7f62f666c4e01f9d0de3aaffaa11f112f8f636.pdf).

The ocean schedule covers departures from Hai Phong in February 2024. Ocean endpoints use five-character port codes. The transit-day pair gives the published minimum and maximum. The April 2019 air announcement uses three-letter airport codes and weekly frequencies. A simulator that assigns any supplier to a port or service should record that assignment as a scenario assumption. The carrier sources establish service availability at the stated dates.

## Acquiring and checking data

The Cala research archive holds raw API responses, supplier candidates, and relationship leads locally under `.cala-private/`. The public snapshot uses independently extracted facts from the source documents. [Cala terms](https://www.cala.ai/terms)

Run `python data/tools/validate_cala_pack.py` from the repository root to verify the offline snapshot. To repeat the expansion using your Cala account, set `CALA_API_KEY` in the environment and run `python data/tools/collect_cala.py`. This consumes account credits for uncached requests.

The manifest hashes the exact UTF-8 bytes of both JSON data files. Its pack hash uses the sorted `path:sha256` lines joined with LF and no final newline. Each source fingerprint covers the retrieved excerpt or the extracted lines used for that source.
