# Railway deployment

Deploy the `oncall@1` incident-response demo as one service. The image sets
`CRUCIBLE_ENVIRONMENTS=oncall` so the registry exposes only this environment, while normal
local installs continue to load both oncall and SupplyGym. The root `Dockerfile` builds its
React UI and Python engine, and FastAPI serves both from the same origin. The engine-only
Dockerfile remains available at `engine/Dockerfile` for API and Harbor use.

## Service settings

1. Connect the GitHub repository and select the branch containing the root `Dockerfile` and
   `railway.json`.
2. Set **Root Directory** to `/` (the repository root), not `/engine` or `/web`.
3. Use `/railway.json` as the config file if a custom config path was previously set.
   The file selects the Dockerfile builder and `/healthz` health check.
4. Clear previous custom build and start commands and any `RAILWAY_DOCKERFILE_PATH` override.
   The image supplies its start command and listens on `0.0.0.0:$PORT`.
5. Keep **one replica**, with sleeping/serverless disabled for live runs. Sessions and their
   session state is in process memory; restarts discard active sessions.
6. Deploy, then use **Networking → Generate Domain** to obtain the public HTTPS URL. If Railway
   asks for a target port, use the service's `PORT` value (8000 is the image fallback).

No API key is required to boot, play manually, or compare built-in policies. Set `MODE=api`
if the service has an old sidecar-mode override.

## Model evaluations and public access

Current main runs model evaluations through Harbor + OpenCode and Docker; there is no direct
model API runner. This Railway image supports **Play** (manual incident response), the session
API/MCP endpoints, and built-in policy comparisons. Select **Play** after opening the demo;
the UI initially opens Watch, which requires the worker described below.

To enable Watch launches, connect a Docker-capable worker through `HARBOR_WORKER_URL` and
`HARBOR_WORKER_TOKEN`. The public app forwards Harbor launch/status, live trial, and saved-run
requests to it. Human play and comparisons stay on the public app. See
[Railway Sandbox worker setup](railway-harbor.md) for the setup script, provider credentials,
approved models, and limits.

The normal service image does not run a Docker daemon. Railway Sandboxes include Docker, so
the worker can remain on Railway. Provider keys stay on the worker; the shared worker token
stays in server variables. The worker rejects unauthenticated requests and exposes only Harbor
and saved-run routes (plus its health check). Public demo runs are shared across visitors.

## Saved transcripts

The default `RUNS_DIR=/tmp/runs` is writable but ephemeral. To retain transcripts across
deployments, attach a Railway volume (for example at `/data`) and set `RUNS_DIR=/data/runs`.
Verify that the container user (UID 10001) can write to the mounted directory. A volume
preserves files, not active in-memory sessions.

The image sets `HARBOR_JOBS_DIR=/tmp/runs/harbor-jobs` and `HARBOR_EXPORTS_DIR=/tmp/runs`
so the non-root user can access writable paths. Override these when supplying saved trials
and exports on a volume. Keep exports private: they contain grading credentials and salts.

## Local check

From the repository root:

```sh
docker build -t crucible-demo .
docker run --rm -p 8000:8000 crucible-demo
```

Open `http://localhost:8000/`. `/healthz` should return `ok`, `/ui/meta` should list `oncall@1`,
and creating a human session should work without an API key.

If Railway reports `No start command detected`, it is still using Railpack instead of the
root Dockerfile. Check the deployed branch, root directory, config path, and builder.

References: [Dockerfile builds](https://docs.railway.com/builds/dockerfiles) and
[configuration reference](https://docs.railway.com/config-as-code/reference).
