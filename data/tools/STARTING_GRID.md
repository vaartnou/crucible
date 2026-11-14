# Rebuilding the Cala starting grid

The public fragment in `data/packs/cala/starting_grid_v0` contains an independently verified subset of the local Cala research grid. Both use the v0.2 contract's canonical JSON, geography and provenance conventions. The two grid tables extend that contract. They are not direct inputs to the existing world-facts validator or simulator.

From the repository root:

```sh
python data/tools/validate_starting_grid.py data/packs/cala/starting_grid_v0
```

To reproduce a build, use the original PR #17 archive (containing the private alias map, reviewed network links and expansion packets) and the starting-grid checkout's ignored `.cala-private` directory. The latter contains the new response packets, reviewed field selections, and cached public sources, including the GeoNames cities1000 and UN/LOCODE reference files. Pass the PR #17 archive directory as the positional argument to the builder.

```sh
python data/tools/build_starting_grid.py PATH_TO_PR17_PRIVATE_ARCHIVE --public
python data/tools/build_starting_grid.py PATH_TO_PR17_PRIVATE_ARCHIVE
python data/tools/validate_starting_grid.py .cala-private/starting_grid_v0
```

The second build goes to `.cala-private/starting_grid_v0`. Building and validating run offline. A clean checkout can validate the public pack without either archive. The full-data rebuild needs the private research inputs. Real names, unrounded addresses, response text and alias mappings stay in the ignored archive.

`collect_starting_grid.py ARCHIVE PLAN` sends only uncached requests to Cala. The private plan is a JSON array of endpoint/input pairs. The shared attempt ledger reserves a credit before each request, including retries. The worker pool spaces starts at least 6.3 seconds apart. A non-retryable HTTP error stops collection, and a retryable 429 respects Retry-After. A transport failure remains charged against the local cap even when billing is unknown. Set `CALA_API_KEY` in the calling environment. Registry verification queries GLEIF directly without credentials.

A company count is a distinct headquarters node. A city marker represents a rounded city reference point shared by every mapped record there. The map groups coincident points and lists the aliases. The basemap uses online tiles; the marker table works offline. Confidence is an editorial rating of the retained evidence. Unknown location, status, product, capacity and corporate fields are JSON nulls. For observed employee counts, size bands are under 50, 50–249, and 250 or more. A reported operator is separate from legal ownership. A scheduled route records service availability at the observation date.

Sources identify specific registry entries, public operator releases or public reference records. References are for review and can identify companies. The map and node/edge identifiers use aliases. The coverage and 20-item sample are generated from each snapshot.
