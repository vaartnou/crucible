# Railway deployment

Deploy the `oncall@1` incident-response demo as one service. The production package ships only
this environment. The root `Dockerfile` builds its React UI and Python engine, and FastAPI
serves both from the same origin. The engine-only Dockerfile remains
available at `engine/Dockerfile` for API and Harbor use.

## Service settings

1. Connect the GitHub repository and select the branch containing the root `Dockerfile` and
   `railway.json`.
2. Set **Root Directory** to `/` (the repository root), not `/engine` or `/web`.
3. Use `/railway.json` as the config file if a custom config path was previously set.
   The file selects the Dockerfile builder and `/healthz` health check.
4. Clear previous custom build and start commands and any `RAILWAY_DOCKERFILE_PATH` override.
   The image supplies its start command and listens on `0.0.0.0:$PORT`.
5. Keep **one replica**, with sleeping/serverless disabled for live runs. Sessions and their
   background model loops are in process memory; restarts discard active sessions.
6. Deploy, then use **Networking → Generate Domain** to obtain the public HTTPS URL. If Railway
   asks for a target port, use the service's `PORT` value (8000 is the image fallback).

No API key is required to boot, play manually, or watch built-in policies. Set `MODE=api`
if the service has an old sidecar-mode override.

For the current Gemini configuration, set `GOOGLE_GENERATIVE_AI_API_KEY` in Railway service
variables. Other OpenAI-compatible providers can use their own server-side key variables,
with the corresponding base URL and model ID in Live Watch. Do not commit secrets or add
them to frontend build variables.

## Before exposing funded model runs publicly

The current workbench is designed for trusted users. Its session request accepts `base_url`
and `api_key_env`, then sends that server key to the supplied URL. A public caller can change
these fields even if the UI hides them. Enforce server-side provider/key/model mappings before
sharing a funded deployment, and add run/concurrency quotas and a provider-side spend cap.
The current routes also share session listings and controls across users; add session ownership
or access control if visitors need private, independent sessions.

## Saved transcripts

The default `RUNS_DIR=/tmp/runs` is writable but ephemeral. To retain transcripts across
deployments, attach a Railway volume (for example at `/data`) and set `RUNS_DIR=/data/runs`.
Verify that the container user (UID 10001) can write to the mounted directory. A volume
preserves files, not active in-memory sessions.

The hosted container does not include a Docker daemon or local Harbor jobs. Human play,
built-in policies, and direct model runs work independently of Harbor monitoring.

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
