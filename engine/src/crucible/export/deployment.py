"""Validated environment-owned application assets for local and Harbor deployments.

Only the deployment runner owns services. Episode runtimes connect to them. Bundle
files and private connection values are never copied into the agent or solution.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml
from pydantic import JsonValue

_RESERVED = frozenset({"main", "mcp-server"})
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
_SERVICE_KEYS = frozenset(
    {
        "image",
        "build",
        "command",
        "entrypoint",
        "environment",
        "depends_on",
        "healthcheck",
        "expose",
        "working_dir",
        "user",
        "read_only",
        "tmpfs",
        "deploy",
        "init",
    }
)


class BundleError(ValueError):
    """An application bundle cannot be safely composed with Crucible services."""


def _relative(path: str) -> str:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or ".." in parsed.parts or "\\" in path:
        raise BundleError(f"Bundle path must be relative and contained: {path!r}")
    if str(parsed) != path or path == ".":
        raise BundleError(f"Bundle path must be normalized: {path!r}")
    return path


def _compose_literal(value: JsonValue) -> JsonValue:
    """Prevent Compose from interpolating runner-host variables in bundle values.

    Docker Compose treats dollars specially even in quoted YAML strings. Escaping
    happens only while rendering Compose; fingerprints and packaged files retain
    their literal inputs, and container shells receive the original dollar signs.
    """
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [_compose_literal(item) for item in value]
    if isinstance(value, dict):
        return {key: _compose_literal(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class DeploymentBundle:
    """Application-only services and files, with private sidecar connection values.

    Service images must use immutable digests. Builds refer only to packaged paths.
    Host mounts, persistent volumes, ports, privileged mode and external networks
    are intentionally unsupported. Service resources use Compose `deploy.resources`.
    """

    services: dict[str, dict[str, JsonValue]]
    files: dict[str, bytes] = field(default_factory=lambda: dict[str, bytes](), repr=False)
    sidecar_environment: dict[str, str] = field(
        default_factory=lambda: dict[str, str](), repr=False
    )
    readiness_services: tuple[str, ...] = ()
    source_fingerprints: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    def validate(self) -> None:
        if not self.services:
            raise BundleError("A deployment bundle must contain services")
        for path in self.files:
            _relative(path)
        for name, service in self.services.items():
            if not _NAME.fullmatch(name) or name in _RESERVED:
                raise BundleError(f"Reserved or invalid service name: {name!r}")
            if unsupported := service.keys() - _SERVICE_KEYS:
                raise BundleError(f"Unsupported service keys for {name}: {sorted(unsupported)}")
            if ("image" in service) == ("build" in service):
                raise BundleError(f"{name} must specify exactly one of image or build")
            if "image" in service:
                image = service["image"]
                if not isinstance(image, str) or not _IMAGE.fullmatch(image):
                    raise BundleError(f"{name} image must be pinned by sha256 digest")
            if "build" in service:
                build = service["build"]
                if not isinstance(build, dict) or set(build) - {"context", "dockerfile"}:
                    raise BundleError("Build must contain only context and optional dockerfile")
                context = build.get("context")
                if not isinstance(context, str):
                    raise BundleError("Build context must name a packaged directory")
                if context != ".":
                    _relative(context)
                dockerfile = build.get("dockerfile", "Dockerfile")
                if not isinstance(dockerfile, str):
                    raise BundleError("Dockerfile must be a relative path")
                _relative(dockerfile)
                target = dockerfile if context == "." else f"{context}/{dockerfile}"
                if target not in self.files:
                    raise BundleError(f"Build Dockerfile is not packaged: {target}")
            dependencies = service.get("depends_on", {})
            if isinstance(dependencies, list):
                names = dependencies
            elif isinstance(dependencies, dict):
                names = list(dependencies)
            else:
                raise BundleError("depends_on must be a list or mapping")
            if any(not isinstance(dep, str) or dep not in self.services for dep in names):
                raise BundleError("Application dependencies must name bundle services")
            resources = service.get("deploy", {})
            if not isinstance(resources, dict) or set(resources) - {"resources"}:
                raise BundleError("Only deploy.resources is supported")
        for name in self.readiness_services:
            if name not in self.services or "healthcheck" not in self.services[name]:
                raise BundleError(f"Readiness service requires a healthcheck: {name}")
        reserved_env = {
            "MODE",
            "SIDECAR_TASK",
            "SIDECAR_SEED",
            "SIDECAR_SALT",
            "GRADE_TOKEN",
            "RUNS_DIR",
        }
        if reserved_env.intersection(self.sidecar_environment):
            raise BundleError("Bundle cannot override reserved sidecar configuration")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in self.sidecar_environment):
            raise BundleError("Invalid sidecar environment key")

    def fingerprint(self) -> dict[str, JsonValue]:
        """Hashes, without exposing private configuration contents."""
        self.validate()
        config = json.dumps(
            {
                "services": self.services,
                "sidecar_environment": self.sidecar_environment,
                "readiness_services": self.readiness_services,
            },
            sort_keys=True,
        )
        return {
            "configuration_sha256": hashlib.sha256(config.encode()).hexdigest(),
            "files": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(self.files.items())},
            "sources": dict(self.source_fingerprints),
        }

    def compose(self, environment: Path) -> None:
        """Package private assets and merge services into a generated Harbor environment."""
        self.validate()
        path = environment / "docker-compose.yaml"
        config = yaml.safe_load(path.read_text())
        services = config["services"]
        if set(services).intersection(self.services):
            raise BundleError("Application service conflicts with an existing service")
        private_network = "crucible-backend"
        if private_network in config.get("networks", {}):
            raise BundleError("Backend network already exists")
        config.setdefault("networks", {})[private_network] = {"internal": True}
        for name, raw in self.services.items():
            service = json.loads(json.dumps(raw))
            if "build" in service:
                context = service["build"]["context"]
                service["build"]["context"] = (
                    "./backend-assets" if context == "." else f"./backend-assets/{context}"
                )
            service["networks"] = [private_network]
            services[name] = service
        sidecar = services["mcp-server"]
        sidecar["networks"] = ["default", private_network]
        sidecar.setdefault("environment", {}).update(self.sidecar_environment)
        sidecar.setdefault("depends_on", {}).update(
            {name: {"condition": "service_healthy"} for name in self.readiness_services}
        )
        for rel, content in self.files.items():
            target = environment / "backend-assets" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # Escape only injected values. Existing Crucible/Harbor templates keep
        # their current behavior, and bundled shell/source files remain verbatim.
        for name in self.services:
            services[name] = _compose_literal(services[name])
        sidecar["environment"].update(
            {key: value.replace("$", "$$") for key, value in self.sidecar_environment.items()}
        )
        path.write_text(yaml.safe_dump(config, sort_keys=False))
