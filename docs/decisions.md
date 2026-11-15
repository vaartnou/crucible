# Decisions

- 2026-11-15 — Ship only `oncall@1`, a deterministic simulator. Remove incident, Fleetbase, backend probe and application deployment machinery. Previous designs and scores remain in Git history.
- 2026-11-15 — Keep `guess@1` under `engine/tests/fixtures/guess`; tests register it explicitly. It is excluded from the production wheel and entry points.
- 2026-11-15 — Use public-tool policies as solvability checks. Do not declare difficulty from bot scores: measure Flash and Pro against graph_walk on salted seeds.
- 2026-11-15 — Keep experimental oncall scores out of golden score tables until difficulty gates stabilize. Retain deterministic, reward-bound, leakage and exploit checks during development.
- 2026-11-15 — Keep the feedback loop small: bots, transcript replay, direct model filtering, then Harbor. Broaden faults only after inspecting model transcripts and measuring the gap.
- Python 3.14, uv, ruff, strict pyright and pytest are the engine toolchain. Environments register through entry points; policies receive observations only. Invalid tool calls count against the episode call budget.

- 2026-11-15 — Oncall `solved` means sustained verified recovery with all diagnosis fields correct. Impact reward remains separate: a late successful repair can score below 0.75. Difficulty gates continue to compare raw mean rewards (graph_walk >= 0.75, reference >= 0.85); this semantic change does not lower those gates.
