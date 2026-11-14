# Plan: Tracks B and C for `fleetbase_dispatch@1` v2

Follows [fleetbase-dispatch-v1-hard.md](fleetbase-dispatch-v1-hard.md). Steps 0–5 are done
(PR vaartnou/crucible#35). Track A passes on 50 dev seeds with the frozen defaults.

## Local constraints (checked 2026-11-15)

| Item | State |
|---|---|
| Harbor | 0.26.0 at `~/.local/bin/harbor` |
| OpenCode | not installed on the host; Harbor installs it in the agent container (pinned 1.19.5 in `fleetbase_pilot.py`) |
| Docker | 29.9.3, VM has **8 GB RAM**, 18 CPUs |
| PHP | not installed on the host; the PHP scripts run inside the app container |
| Credentials | repo-root `.env` holds only `GOOGLE_GENERATIVE_AI_API_KEY` |

Consequences:
- **"Stronger model" is a Gemini Pro model**, because no other provider key exists. Pick the
  strongest `google/gemini-*` model id that OpenCode 1.19.5 accepts, after one cheap probe call.
- **Native concurrency is 1.** One stack needs about 4 GB, and the VM has 8 GB.
  Track B runs use no Fleetbase stack, so they can use concurrency 3.
- Job and run folders go to the gitignored `jobs/` and `runs/` at the repository root, and exports go to `harbor/`.
  The only things committed are the scores, the logs and any code fixes.

## Track B: agent on `fleetbase_sim@1`

1. Export dev seeds 0–2: `crucible export harbor --task fleetbase_sim@1 --seeds 0-2 --out ../harbor/fleetbase-sim-v2`.
2. Sanity runs: `-a oracle` (the reward must equal the in-process reference on the same salted world)
   and `-a nop` (reward 0).
3. Agent runs: `-a opencode` with `google/gemini-3.8-flash` and the Pro model, on all 3 seeds, `-n 3`.
4. For each trial, record the reward, the in-process reference and greedy on the same salted world
   (call the sweep or the policy code with the export's salt), the gap, the calls used, the stop reason,
   and one or more failure tags: missed cancellation, never replanned, ran out of budget,
   overloaded, ignored forecast, or other (described).
   Read the tags from the transcript and the grading evidence.
5. Write `runs/fleetbase-sweep-log.md` (gitignored, so the full log lives there) and a summary
   section in `docs/harbor-runs.md` (committed).
6. Gate: the agent is at least 0.15 below the reference on all 3 seeds, for both models.
   If the gate fails, **do not retune**. Report which seeds fail and why. The retuning decision
   goes back to the user.

## Track C: native confirmation

1. Build and start one stack from the export of `fleetbase_dispatch@1` seed 0. Run
   `fleetbase_smoke.py --harbor`. It runs `native-proof.php` first and checks each
   unverified v2 endpoint assumption.
2. If an assumption fails, implement the meta fallback named in `docs/fleetbase-api-audit.md`,
   fix the offline fake to match the real behaviour, re-run the offline tests, and
   then re-run the proof. Mark each confirmed assumption as verified in the audit doc.
3. Run live parity: `pytest test_parity.py -k live` against the running stack, and then `count-state.php` (it must exit 0).
4. Run a clean-stack episode, reset-and-replay, cleanup counts and the Harbor oracle, as the smoke runner gates them.
5. Write a new `docs/fleetbase-native-proof.json` for v2. Its count-helper hash must match the current `count-state.php`.
6. **Pilot. Run it only after Track B passes.** `fleetbase_pilot.py freeze` creates a new experiment identity
   with 3 fresh salted worlds. `run` then executes the ladder (reference, greedy, do_nothing, stop_early)
   and the agent (`gemini-3.8-flash`) on native, with concurrency 1 and one Compose project per trial.
7. Record the results in `docs/fleetbase-validation.md`. Destroy every stack and confirm with `docker ps -a`
   and `docker volume ls` that nothing is left over.

## Order

Track B and Track C steps 1–5 run in parallel, because they don't depend on each other and Track B uses little RAM.
Track C step 6 waits for the Track B gate.
