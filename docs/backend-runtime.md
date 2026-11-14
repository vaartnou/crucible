# Backend runtime

`crucible.backends` provides private HTTP connection management and episode leases.
It contains no application-specific operations. Local runners and Harbor start and
stop services; the runtime connects to them. `ownership="managed"` records the
runner's service ownership but never grants the episode permission to stop a
service. An attached installation remains running after the episode closes.

## Adapter contract

```python
from pathlib import Path
from pydantic import SecretStr
from crucible.backends import (
    BackendConnection, BackendEpisode, ExclusiveLeaseProvider, HttpBackendRuntime,
)

async def prepare(context: BackendEpisode) -> None:
    # Create only episode-owned records. Record their native IDs for cleanup.
    response = await context.client.post(
        "records", json={"episode": context.resource.episode_id},
    )
    response.raise_for_status()

async def release(context: BackendEpisode) -> None:
    # Remove only those records; tolerate an interrupted preparation.
    response = await context.client.delete(f"records/{context.resource.episode_id}")
    response.raise_for_status()

runtime = HttpBackendRuntime(
    BackendConnection(
        deployment_id="local-backend-installation",  # consistent across URL aliases
        base_url=SecretStr("http://application:8080/v1/"),
        headers={"Authorization": SecretStr("Bearer PRIVATE_TOKEN")},
        exclusive_access=True,
    ),
    ExclusiveLeaseProvider(Path("/private/runner/backend-locks")),
    readiness_path="/healthz",  # relative to the configured base URL
    prepare=prepare,
    release=release,
)
context = await runtime.acquire()
# Adapter operations use context.client. Public tools return selected business fields.
# Task.aclose() must await runtime.aclose(), including after a failed reset.
await runtime.aclose()
```

`acquire()` obtains isolation before making any HTTP request or invoking preparation.
It waits for readiness, then invokes the adapter's `prepare` hook. Failure or
cancellation at readiness or preparation closes the client and releases the lease.
The adapter's `release` hook also runs after partial initialization, so it must be
idempotent. A runtime is single-use; construct a new runtime for a new episode.

`ready()` can repeat an HTTP readiness check on an active lease. It accepts only
local HTTP paths and rejects redirects. Each attempt has a total timeout as well
as HTTPX phase timeouts; attempts and intervals are bounded. A health endpoint
must represent application readiness, including migrations and required services,
not merely a listening TCP socket.

`BackendRuntime` is a protocol with `acquire`, `ready`, `collect_artifacts`, and
`aclose`; an environment can implement a different backend transport without
changing Crucible core. `BackendEpisode` holds a private client and `ResourceLease`
with `episode_id`, resource `namespace`, and `isolation` kind.

## Isolation and ownership

The default `ExclusiveLeaseProvider` uses a nonblocking POSIX `flock`. All local
processes targeting the same installation must use the same private lock
directory and `deployment_id`, even if they connect through different URLs. A
conflict raises `LeaseConflict` before requests or mutations. It and readiness
failures are `BackendUnavailable`, an `InfraError` the executor retries. Lock files stay on
disk after release: removing them could let two processes lock different inodes.
The OS also releases the lock when a process exits.

This protects cooperating processes on a shared filesystem; it does **not** prove
that an unrelated user or another machine cannot modify a remote installation.
`exclusive_access=True` is the deployment owner's explicit assertion of exclusive
tenancy, in addition to the enforced process lease. Use a dedicated evaluation
installation with access restricted to its runner. Do not enable the flag for an
ordinary shared production installation. The default false value rejects access.
For multiple hosts, use a common lock filesystem with verified lock semantics or
an application-enforced isolation provider.

`LeaseProvider` is the extension point for isolated tenants/databases. Its
`acquire(connection, episode_id)` must atomically allocate and verify isolation,
then return a `ResourceLease` with a private release callback. An episode label or
a random ID inside shared records is not an isolation guarantee. Providers must
not return a lease until allocation succeeds and must clean up partial allocation.
Application adapters seed records; simulator bridges apply physical events.
Neither may use the runtime's release hook to wipe an attached installation.

`aclose()` is idempotent and awaits a shared cleanup task. It tries adapter record
release, client close, and lease release even if an earlier operation fails.
Cancellation cannot skip the remaining release stages. Errors are logged and
recorded as sanitized exception type names in `runtime.cleanup_errors`, so an
adapter or evidence writer can include them in private episode diagnostics.
Cleanup does not replace an original initialization error. Hooks must use bounded
I/O and handle their own application-specific retry/read-back rules.

## Private artifacts and credentials

Connection URLs and headers use `SecretStr`, are absent from object representations,
and serialize as masked values. Validation errors hide input values. Keep
`BackendConnection`, `BackendEpisode`, runtime exceptions, and HTTP responses out
of public tool results. Adapters must project only documented operational fields;
the runtime cannot decide which fields are legal observations for an application.

An optional async `artifacts(context)` hook returns `Mapping[str, bytes]` containing
only the episode's evidence. Call `collect_artifacts(private_directory)` **before**
closing the runtime. It validates relative names, rejects traversal and symlinks
that escape the destination, writes private files outside the event loop, redacts
configured header values, and returns relative names with SHA256 hashes. The
collector must exclude secrets itself, including any tokens appearing outside
complete header values. Never collect whole configuration files or shared database
dumps. Keep the destination private and inaccessible to the agent; a hostile
process racing filesystem writes is outside the local-runner trust boundary.

## Contract verification

`engine/tests/backends/test_runtime.py` starts a separate FastAPI/Uvicorn process
on a dynamically bound loopback socket. Tests prepare and inspect business state,
collect hashed evidence, release records and the HTTP client, and confirm that
the attached application remains available. Additional checks cover cross-process
lease conflicts, unproven access, readiness failure, cancellation, partial startup,
repeated cleanup, artifact traversal, and secret masking. They require local
loopback access; a sandbox that prohibits socket binding must run these tests with
the same network permission as other repository HTTP integration tests.
