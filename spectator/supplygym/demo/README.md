# Recorded SupplyGym demonstration

These are local, in-process SupplyGym runs from PR 37 head
`dba5c4907dc9d937a01f3fb292d7d800a99987af`. The PR's `EpisodeSession`, four
baseline policies, and `Spectator` produced each 53-week stream and final
score. All five runs use seed 62000 and the same deliberately public demo salt;
they are demonstration data, not a private evaluation or a Harbor/model run.

The bundled records use `supplygym@3` as their task display alias. Scenario IDs,
version fingerprints, decisions, and financial results retain their original values.

`scripted_buyer` is a deterministic buyer: it sets safety stock to 1.5 days
and requests 30% expediting when the public supplier view reports any late
orders, otherwise 0%. It does not call an LLM. For example, the week 5
observation reports four late orders and the script requests 30% expediting;
week 7 reports none and requests 0%.

To regenerate, check out the cited PR head, install its engine dependencies,
and run `generate.py <path-to-demo-runner>` with Python 3.14 and that checkout's `engine/src` and
`engine/scripts` on `PYTHONPATH`. Event timestamps will change. The fixed
salt is included in the generator so this public demonstration is reproducible;
real evaluation salts must remain private. Paths in the manifest are relative
to this directory.
