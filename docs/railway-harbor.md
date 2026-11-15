# Harbor evaluations on Railway

Use two components in the same Railway project:

- A normal Railway service builds the root Dockerfile and serves the public incident demo.
- A Railway Sandbox runs the Harbor worker natively, with its own Docker daemon. The public
  service forwards Harbor requests to the worker over authenticated HTTP. Visitors use the
  same Watch and Runs UI; no worker token or provider key is sent to their browser.

Railway documents Docker in [Sandboxes](https://docs.railway.com/sandboxes). A normal service
does not provide the Docker host required by this app's multi-container trials. A Linux VPS
with Docker works as the worker too.

## Create the Sandbox

Use the current Railway CLI, log in, and link the intended project/environment. Create a
local `.env.worker` file (ignored by Git) with:

```dotenv
HARBOR_WORKER_TOKEN=replace-with-a-random-token-of-at-least-32-characters
HARBOR_ALLOWED_MODELS=google/gemini-3.8-flash
HARBOR_API_KEY_NAME=GOOGLE_GENERATIVE_AI_API_KEY
GOOGLE_GENERATIVE_AI_API_KEY=replace-with-your-provider-key
HARBOR_MAX_CONCURRENCY=1
HARBOR_DAILY_JOB_LIMIT=10
```

Generate the worker token locally with `python3 -c 'import secrets; print(secrets.token_hex(32))'`.
Use the same token in the public service. Replace the model list with the OpenCode provider/model
IDs enabled for your accounts, separated by commas; set the matching provider keys on the
worker. The UI shows this list as a dropdown. Put a spending cap on the demo provider account.
`HARBOR_API_KEY_NAME` names the credential variable Harbor must forward to OpenCode. It defaults
to `GOOGLE_GENERATIVE_AI_API_KEY` in the serve script; set it explicitly for another provider.
The key value is inherited privately and is never placed in command arguments.

```sh
railway sandbox create --private-network --domain 8080 --idle-timeout-minutes 0 --env-file .env.worker
railway sandbox ssh
```

Disabling idle destruction requires Hobby or Pro and keeps the Sandbox billable until
destroyed. Sandbox compute is billed separately from ordinary services. Choose enough memory
for the worker, agent, simulator, and image builds; start with one concurrent trial and measure.

Inside the Sandbox, check out the same revision as the public app into a directory named
`crucible`. Use a read-only deploy key if the repository is private. Ensure `git` and
[uv](https://docs.astral.sh/uv/getting-started/installation/) are installed. Docker is supplied
by the Sandbox; check `docker info` and `docker compose version` before proceeding.
Some Sandbox images omit the Compose and Buildx CLI plugins. Install both from Docker's
official [Compose](https://docs.docker.com/compose/install/linux/) and
[Buildx](https://github.com/docker/buildx#installing) instructions if `docker compose version`
or `docker buildx version` fails. The Docker daemon alone is insufficient for Harbor builds.

```sh
cd crucible
bash infra/harbor-worker.sh prepare
```

The script installs Harbor 0.26.0 in an isolated Python 3.13 tool environment, installs the
locked engine with Python 3.14, exports seeds 0–2, and builds the simulator sidecar image.
The demo agent image preinstalls OpenCode 1.19.5 with Node 22 so each trial can reuse Docker
build layers instead of installing Node and OpenCode during startup. Normal Harbor exports
keep their original agent image. Re-running prepare preserves
the existing exports and salts. Set `HARBOR_SEEDS=0` before prepare for a single-trial demo.

Exit SSH, then launch a managed background command from your computer (adjust the checkout
path if needed):

```sh
railway sandbox exec --detach -- bash crucible/infra/harbor-worker.sh serve
railway sandbox list
```

Record the session name for logs (`railway sandbox exec --session SESSION_NAME`). Run only
one worker process. The worker listens on port 8080 and starts with one concurrent trial and
10 job launches per UTC day. The daily limit counts job directories, survives worker process
restarts, and is global across demo visitors. Set explicit environment values to adjust it;
`HARBOR_DAILY_JOB_LIMIT=0` disables the limit.

## Connect the public service

Set these variables on the ordinary Railway service and redeploy:

```dotenv
HARBOR_WORKER_URL=https://the-sandbox-domain-from-railway
HARBOR_WORKER_TOKEN=the-same-random-token-as-the-worker
```

Do not set `HARBOR_WORKER_MODE` on the public service. The setup script sets it only on the
worker. `HARBOR_WORKER_URL` accepts an HTTPS origin, or HTTP for loopback / Railway internal
hosts. Use the Sandbox's published HTTPS domain for the setup above; auth is required even
though that endpoint has a public address.

Open Watch: it should show the prepared oncall export, approved models, and an enabled Start
evaluation button. Start one trial, select its live container, and open its transcript in Runs
after completion. Wrong worker credentials return 401; an unreachable worker returns 503.
Manual play stays available when the worker is offline.

## Data and lifecycle

Worker jobs and exports live under `crucible/runs/worker` by default. They remain there when
the worker process restarts, but destroying the Sandbox removes them. Copy results out before
destroying it, and keep the original salted exports private. Checkpoints may contain provider
credentials and grading secrets; treat them as private too. Public service volumes do not
preserve worker data. Sandbox recreation requires updating the worker URL and rerunning serve.

This implements a shared public demo, not private per-user runs. The server enforces the
approved model list and concurrency limit. Provider spending caps bound inference costs;
Railway usage limits bound infrastructure costs. The token gives the public server access to
worker operations and must never be put into frontend variables.

References: [Sandbox lifecycle and pricing](https://docs.railway.com/sandboxes),
[Sandbox CLI](https://docs.railway.com/cli/sandbox), and
[Harbor installation](https://docs.harborframework.com/getting-started/installation).
