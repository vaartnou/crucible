# External backend proof (`backend_probe@1`)

This is a small infrastructure fixture, not Fleetbase and not a performance
benchmark. It is new code from `main`; it does not import or preserve the former
`dispatch@1` or `dispatch@2` environments.

The application is a separate FastAPI service. The task acquires a private HTTP
connection and an exclusive installation lease before preparing an episode-owned
record. Public tools dispatch the record, advance virtual time, inspect progress,
and finish. A private bridge observes committed dispatch, simulates a 10-unit trip
at one unit per minute, and synchronizes verified arrival back to the application.
The grader trusts that physical event, never the application's status alone.
Value is 100; cost is one per traveled unit; a completed trip earns utility 90 and
reward 0.9. Stopping freezes movement immediately. Duplicate dispatch and progress
synchronization do not create more delivery value.

`Task.aclose()` removes only the episode record and releases its connection and
lease. The application remains alive. The deployment runner owns the process or
containers and stops them after the run. Credentials and application connectivity
exist only in the private sidecar network and configuration.

## Local proof

From `engine/`, with dependencies installed:

```sh
.venv/bin/python scripts/backend_probe_smoke.py
```

The script starts an independent uvicorn process, runs reference and no-op
operators sequentially, verifies that both episode records were removed while the
application remains healthy, saves private evidence under
`engine/runs/backend-probe-local/`, and terminates the process. Use a fresh output
directory for each run; an existing directory is rejected to prevent stale proof.

To attach manually, start `uvicorn --factory
crucible.envs.backend_probe.app:create_app` with a private
`BACKEND_PROBE_TOKEN`. Configure the same token, `BACKEND_PROBE_URL` and a shared
`BACKEND_PROBE_LOCK_DIR` for Crucible. Every process connecting to the same
installation must use the same deployment identity and lock directory. The
fixture asserts exclusive access, so do not attach it to a shared installation.
`BACKEND_PROBE_ARTIFACTS` optionally selects a private evidence directory.

## Harbor proof

The bundle contains a pinned Python base, application source, and fully pinned
runtime requirements. The exporter fingerprints these files, the wheel, and the
private configuration. Harbor starts the application and sidecar together; the
task does not invoke Docker.

```sh
uv run --directory engine crucible export harbor --task backend_probe@1 \
  --seeds 0 --out ../harbor/backend-probe-1
harbor run -p harbor/backend-probe-1 -a oracle -o jobs --job-name backend-probe-1-oracle
harbor run -p harbor/backend-probe-1 -a nop -o jobs --job-name backend-probe-1-nop
```

Run the export command from the repository root; its explicit `--out` is resolved
by `uv --directory engine` relative to `engine/`. The generated task directory
includes private credentials and is ignored by Git. Do not share it as an
agent-visible artifact. Verifier output includes `grade.json`, `transcript.json`,
and `evidence.json`; the latter contains the verified event and native snapshot.

## Verified results

Verified on 2026-11-14, Harbor 0.26.0, local Docker. Source baseline
`59b15f25a931bfc7a8f01865abbe4c5c61097911` plus this implementation.
The exported wheel SHA256 was
`5705458ee11d74ef6236e4364afdd0f47697ad1633218d018d4392abc6ace940`.

| Execution | Operator | Reward | Utility | Deliveries | Errors |
|---|---|---:|---:|---:|---:|
| Separate local application process | reference | 0.9 | 90 | 1 | 0 |
| Separate local application process | no-op | 0 | 0 | 0 | 0 |
| Harbor `backend-probe-1-oracle` | reference | 0.9 | 90 | 1 | 0 |
| Harbor `backend-probe-1-nop` | no-op | 0 | 0 | 0 | 0 |

The oracle used four public calls and the verifier recorded one arrival at distance
10 and minute 10, with application status `delivered`. Both Harbor jobs completed
without retries or infrastructure exceptions. Their project containers were
removed after completion. Local proof verified deletion of both episode records
and then terminated its application process.

These results validate the runtime boundary only. They do not establish native
Fleetbase behavior, benchmark difficulty, or any model-performance gap.

## Tests

The explicit integration tests use real localhost HTTP with isolated uvicorn
servers: completion/evidence/cleanup, semantic replay, no-op zero, duplicate
commitments, conflicting leases, and status-forgery resistance. They require
permission to bind localhost sockets. Generic offline checks and historical
in-process golden scoring exclude this external-service fixture because no
application deployment exists in those tests; they are not replaced with mocks
or fabricated golden scores.
