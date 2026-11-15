# Application deployment bundles

`Task.deployment_bundle()` returns `None` for ordinary tasks, preserving existing
export templates. Application-backed tasks can return a
`crucible.export.deployment.DeploymentBundle`: services, packaged files, private
sidecar environment values and required healthy services.

Harbor 0.26.0 owns service startup and shutdown. Tasks connect through the backend
runtime and never start nested Compose. Local runners may use the same application
services with a unique Compose project name.

Composition reserves `main` and `mcp-server`. Images require SHA-256 digests; build
contexts and Dockerfiles must refer to packaged relative files. Host ports,
container names, mounts, volumes, arbitrary networks, privileged options, and
deploy options other than resources are rejected. Bundle services use an internal
private network; only the MCP sidecar joins both that network and Harbor's default
network. Required services declare health checks. Files are copied beneath
`environment/backend-assets`; build contexts are rewritten there. Application
files and credentials are never copied into `solution` or the agent image.

Task identity, seed, world salt, grade token and runs folder cannot be overridden.
Bundle service and sidecar strings are literal values: the exporter escapes dollar
signs for Compose so runner-host environment variables cannot change credentials
or commands. Container shells receive the original dollar signs. Packaged files
are copied verbatim, and fingerprints describe the original literal configuration.
Private credentials are runner configuration, not public instructions. Generated
exports contain credentials and must remain untracked. Do not mount the export's
environment folder into the agent. Manifests contain configuration and asset
hashes plus supplied upstream source revisions, without secret values. The normal
exporter records the wheel hash and lock-exported runtime requirements.

Initialization belongs in a packaged entrypoint or an initialization service
with explicit dependency ordering. Per-service resources use `deploy.resources`.
Application snapshots are collected through the backend artifact hook; deployment
runners must additionally retain service logs on infrastructure failures.
