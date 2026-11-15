"""Export a task to Harbor: one task folder per seed.

Each folder holds the agent's container (no engine in it), a simulator sidecar that installs the
engine from a vendored wheel, a verifier that reads `/grade` with a per-task token, and an oracle
`solve.sh` that invokes a token-protected in-process policy. See `docs/decisions.md` for why.
"""

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable, Sequence
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from crucible.core.registry import get_task
from crucible.core.task import TaskSpec

__all__ = [
    "UV_VERSION",
    "EngineArtifacts",
    "ExportError",
    "build_engine_artifacts",
    "export_harbor",
    "task_folder_name",
]

UV_VERSION = "0.12.18"
_PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
_EXECUTABLE = {"solution/solve.sh", "tests/test.sh"}
_EXPORT_KEYWORDS = ("crucible", "mcp", "interactive")
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MB = 2048
DEFAULT_AGENT_TIMEOUT_SEC = 900.0


class ExportError(Exception):
    """The export cannot run (bad output folder, missing engine source, failed build)."""


class EngineArtifacts(BaseModel):
    """The engine wheel and its pinned runtime requirements, vendored into each task."""

    model_config = ConfigDict(frozen=True)

    wheel: Path
    requirements: Path


def task_folder_name(task_ref: str, seed: int) -> str:
    """`incident@1`, seed 3 -> `incident-1-seed-3`."""
    return f"{task_ref.replace('@', '-')}-seed-{seed}"


def _engine_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "pyproject.toml").is_file() or not (root / "uv.lock").is_file():
        raise ExportError(
            "engine source not found next to the installed package; pass --wheel and --requirements"
        )
    return root


def build_engine_artifacts(out_dir: Path) -> EngineArtifacts:
    """Build the engine wheel and export its locked requirements into `out_dir`."""
    root = _engine_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    requirements = out_dir / "requirements.txt"
    commands = [
        ["uv", "build", "--wheel", "--out-dir", str(out_dir), "--quiet"],
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
            "--quiet",
        ],
    ]
    for command in commands:
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise ExportError(f"{' '.join(command[:2])} failed:\n{done.stderr.strip()}")
    wheels = sorted(out_dir.glob("crucible-*.whl"))
    if len(wheels) != 1:
        raise ExportError(f"expected one crucible wheel in {out_dir}, found {len(wheels)}")
    return EngineArtifacts(wheel=wheels[0], requirements=requirements)


def _template_files() -> list[tuple[str, Traversable]]:
    """(relative path, resource) for every template file, sorted by path."""
    found: list[tuple[str, Traversable]] = []

    def walk(node: Traversable, prefix: str) -> None:
        for child in node.iterdir():
            path = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, f"{path}/")
            elif not child.name.startswith(".") and child.name != "__init__.py":
                found.append((path, child))

    walk(files("crucible.export") / "templates" / "harbor", "")
    return sorted(found, key=lambda item: item[0])


def _render(text: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ExportError(f"template placeholder @@{key}@@ has no value")
        return values[key]

    return _PLACEHOLDER.sub(replace, text)


def _toml_str(text: str) -> str:
    """A TOML basic string. JSON string escapes are a subset of TOML's."""
    return json.dumps(text, ensure_ascii=False)


def _keywords(spec: TaskSpec) -> list[str]:
    """Export keywords first, then the task's own, without repeats."""
    return list(dict.fromkeys((*_EXPORT_KEYWORDS, *spec.keywords)))


def _resources(spec: TaskSpec) -> dict[str, str]:
    """`task.toml` values for the task's resources; a field left `None` keeps the default."""
    image = spec.docker_image
    return {
        "CPUS": str(spec.cpus or DEFAULT_CPUS),
        "MEMORY_MB": str(spec.memory_mb or DEFAULT_MEMORY_MB),
        "AGENT_TIMEOUT_SEC": repr(float(spec.timeout_sec or DEFAULT_AGENT_TIMEOUT_SEC)),
        "DOCKER_IMAGE_LINE": f"docker_image = {_toml_str(image)}\n" if image else "",
    }


def _git_commit() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def _prepare_out(out: Path, force: bool) -> None:
    if out.exists() and not out.is_dir():
        raise ExportError(f"{out} exists and is not a folder")
    if out.is_dir() and any(out.iterdir()):
        if not force:
            raise ExportError(f"{out} is not empty; pass --force to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def export_harbor(
    task_ref: str,
    seeds: Sequence[int],
    out: Path,
    artifacts: EngineArtifacts,
    *,
    solution_policy: str = "reference",
    force: bool = False,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    salt_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    git_commit: str | None = None,
) -> list[Path]:
    """Write one Harbor task folder per seed under `out`, plus `manifest.json`.

    Raise `ExportError` if `out` is not empty and `force` is false. Every task gets its own
    grade token; the token is written only to `[verifier.env]` and the sidecar's environment.
    Every task also gets its own world salt, written only to the sidecar's environment, so the
    world cannot be rebuilt from the seed in the folder name.
    """
    if not seeds:
        raise ExportError("no seeds given")
    if len(set(seeds)) != len(seeds):
        raise ExportError("seeds repeat")
    task = get_task(task_ref)
    spec = task.spec
    ref = spec.ref
    instruction = task.instruction().strip()
    keywords = "[" + ", ".join(_toml_str(k) for k in _keywords(spec)) + "]"
    commit = git_commit if git_commit is not None else _git_commit()
    wheel_bytes = artifacts.wheel.read_bytes()
    engine_version = artifacts.wheel.name.split("-")[1]
    templates = _template_files()

    _prepare_out(out, force)
    dockerfile = files("crucible.export") / "templates/harbor/environment/mcp-server/Dockerfile"
    image_hash = hashlib.sha256(
        wheel_bytes + artifacts.requirements.read_bytes() + dockerfile.read_bytes()
    ).hexdigest()
    sidecar_image = f"crucible-sidecar:{image_hash[:24]}"
    folders: list[Path] = []
    for seed in seeds:
        folder = out / task_folder_name(ref, seed)
        values = {
            "TASK_NAME": f"crucible/{task_folder_name(ref, seed)}",
            "TASK_REF": ref,
            "SEED": str(seed),
            "TITLE": spec.title,
            "DESCRIPTION": _toml_str(f"{spec.description.strip()} ({ref}, seed {seed})"),
            "KEYWORDS": keywords,
            **_resources(spec),
            "ENGINE_VERSION": engine_version,
            "GIT_COMMIT": commit,
            "GRADE_TOKEN": token_factory(),
            "SOLUTION_TOKEN": secrets.token_hex(32),
            "SIDECAR_IMAGE": sidecar_image,
            "SIDECAR_SALT": salt_factory(),
            "SOLUTION_POLICY": solution_policy,
            "INSTRUCTION": instruction,
            "WHEEL_NAME": artifacts.wheel.name,
            "UV_VERSION": UV_VERSION,
        }
        for rel, resource in templates:
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_render(resource.read_text(), values))
            if rel in _EXECUTABLE:
                target.chmod(0o755)
        for vendor in ("environment/mcp-server",):
            shutil.copyfile(artifacts.wheel, folder / vendor / artifacts.wheel.name)
            shutil.copyfile(artifacts.requirements, folder / vendor / "requirements.txt")
        folders.append(folder)

    manifest: dict[str, JsonValue] = {
        "task_ref": ref,
        "seeds": list(seeds),
        "solution_policy": solution_policy,
        "engine_version": engine_version,
        "git_commit": commit,
        "wheel": artifacts.wheel.name,
        "sidecar_image": sidecar_image,
        "wheel_sha256": hashlib.sha256(wheel_bytes).hexdigest(),
        "tasks": [f.name for f in folders],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    build = out / "build-sidecar.sh"
    build.write_text(
        '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\n'
        f"docker build -t {sidecar_image} ./{folders[0].name}/environment/mcp-server\n"
    )
    build.chmod(0o755)
    return folders
