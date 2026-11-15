"""The `crucible` command line: list tasks and policies, run an eval and print a table."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import BaseModel, ConfigDict
from rich.console import Console
from rich.table import Table

from crucible.agents.remote import HttpEpisode, McpEpisode, RemoteOutcome, drive
from crucible.core.episode import EpisodeResult
from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job, RunSpec, expand, parse_seeds
from crucible.core.registry import get_policy, get_task, list_policies, list_tasks

app = typer.Typer(
    name="crucible",
    help="Run and compare policies on Crucible environments.",
    no_args_is_help=True,
    add_completion=False,
)

_SHARE_FLAGS = ("root_cause_correct", "probes_healthy", "budget_exhausted", "env_error")


class Row(BaseModel):
    """One policy's line in the summary table. A share is `None` when no episode has the flag."""

    model_config = ConfigDict(frozen=True)

    policy: str
    episodes: int
    mean: float
    min: float
    max: float
    shares: dict[str, float | None]


def summarise(results: Sequence[EpisodeResult], policies: Sequence[str]) -> list[Row]:
    """One row per policy, in the order given. Policies with no results get no row."""
    rows: list[Row] = []
    for policy in policies:
        mine = [r for r in results if r.job.policy == policy]
        if not mine:
            continue
        totals = [r.score.total for r in mine]
        shares: dict[str, float | None] = {}
        for flag in _SHARE_FLAGS:
            # The episode loop sets `env_error` only on failure, so a clean run has no flag.
            default = False if flag == "env_error" else None
            seen = [r.score.flags.get(flag, default) for r in mine]
            seen = [v for v in seen if v is not None]
            shares[flag] = sum(seen) / len(mine) if seen else None
        rows.append(
            Row(
                policy=policy,
                episodes=len(mine),
                mean=sum(totals) / len(totals),
                min=min(totals),
                max=max(totals),
                shares=shares,
            )
        )
    return rows


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(2)


def _share(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _summary_table(rows: Sequence[Row]) -> Table:
    table = Table(title="Results")
    table.add_column("policy", no_wrap=True)
    for name in ("episodes", "mean", "min", "max", *_SHARE_FLAGS):
        table.add_column(name, justify="right", no_wrap=True, overflow="fold")
    for row in rows:
        table.add_row(
            row.policy,
            str(row.episodes),
            f"{row.mean:.3f}",
            f"{row.min:.3f}",
            f"{row.max:.3f}",
            *(_share(row.shares[f]) for f in _SHARE_FLAGS),
        )
    return table


def _episode_table(results: Sequence[EpisodeResult]) -> Table:
    table = Table(title="Episodes")
    for name in ("policy", "seed", "repeat", "score", "steps", "stop_reason"):
        table.add_column(name, justify="left" if name in ("policy", "stop_reason") else "right")
    for r in results:
        table.add_row(
            r.job.policy,
            str(r.seed),
            str(r.job.repeat),
            f"{r.score.total:.3f}",
            str(r.steps),
            r.stop_reason,
        )
    return table


def _build_jobs(task: str, policies: list[str], seeds: list[int], repeats: int) -> list[Job]:
    try:
        get_task(task)
        for policy in policies:
            get_policy(policy, task)
        return [
            job
            for policy in policies
            for job in expand(RunSpec(task=task, policy=policy, seeds=seeds, repeats=repeats))
        ]
    except (KeyError, ValueError) as exc:
        _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))


def _default_out(task: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("runs") / f"{task.replace('@', '-')}-{stamp}"


@app.command("tasks")
def tasks_command() -> None:
    """List registered tasks."""
    for ref in list_tasks():
        typer.echo(ref)


@app.command("policies")
def policies_command(
    task: Annotated[str, typer.Option("--task", help="Task ref, e.g. oncall@1.")],
) -> None:
    """List the policies available for a task."""
    try:
        get_task(task)
        names = list_policies(task)
    except (KeyError, ValueError) as exc:
        _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))
    for name in names:
        typer.echo(name)


@app.command("eval")
def eval_command(
    task: Annotated[str, typer.Option("--task", help="Task ref, e.g. oncall@1.")],
    policy: Annotated[str, typer.Option(help="Comma-separated policy names.")] = "reference",
    seeds: Annotated[str, typer.Option(help="Seeds, e.g. '0-9,12'.")] = "0-9",
    repeats: Annotated[int, typer.Option(min=1, help="Episodes per seed.")] = 1,
    concurrency: Annotated[int, typer.Option(min=1, help="Episodes run at once.")] = 1,
    out: Annotated[Path | None, typer.Option(help="Output folder (reuse to resume).")] = None,
    per_seed: Annotated[bool, typer.Option("--per-seed", help="Also print each episode.")] = False,
) -> None:
    """Run policies on a task and print one results table."""
    policies = [p.strip() for p in policy.split(",") if p.strip()]
    if not policies:
        _fail("--policy needs at least one name")
    if len(set(policies)) != len(policies):
        _fail(f"--policy repeats a name: {policy!r}")
    try:
        seed_list = parse_seeds(seeds)
    except ValueError as exc:
        _fail(str(exc))
    jobs = _build_jobs(task, policies, seed_list, repeats)
    out_dir = out if out is not None else _default_out(task)
    results = asyncio.run(LocalExecutor(concurrency=concurrency).run(jobs, out_dir))

    console = Console(width=max(Console().width, 130))  # keep the table readable when piped
    console.print(_summary_table(summarise(results, policies)))
    if per_seed:
        console.print(_episode_table(results))
    console.print(f"Output folder: {out_dir}")
    console.print(f"Summary CSV: {out_dir / 'summary.csv'}", soft_wrap=True)
    if any(r.score.flags.get("env_error", False) for r in results):
        typer.echo("Error: at least one episode ended with env_error", err=True)
        raise typer.Exit(1)


async def _play(
    task_ref: str, policy_name: str, mcp: str | None, api: str | None, seed: int | None
) -> RemoteOutcome:
    task = get_task(task_ref)
    policy = get_policy(policy_name, task_ref)
    if mcp is not None:
        episode, instruction = await McpEpisode.connect(mcp)
        return await drive(policy, episode, task.spec, task.tools(), instruction)
    assert api is not None and seed is not None
    http, instruction = await HttpEpisode.start(task_ref, seed, base_url=api)
    return await drive(policy, http, task.spec, task.tools(), instruction, http.first_observation)


@app.command("play")
def play_command(
    task: Annotated[str, typer.Option("--task", help="Task ref, e.g. oncall@1.")],
    policy: Annotated[str, typer.Option(help="Policy name.")] = "reference",
    mcp: Annotated[str | None, typer.Option(help="MCP URL of a sidecar, e.g. .../mcp.")] = None,
    api: Annotated[str | None, typer.Option(help="Base URL of an API server.")] = None,
    seed: Annotated[int | None, typer.Option(help="Seed (required with --api).")] = None,
) -> None:
    """Play one policy against a running server and print one JSON line."""
    if (mcp is None) == (api is None):
        _fail("give exactly one of --mcp and --api")
    if api is not None and seed is None:
        _fail("--api needs --seed")
    if mcp is not None and seed is not None:
        _fail("--seed only works with --api (a sidecar has its own seed)")
    try:
        get_task(task)
        get_policy(policy, task)
    except (KeyError, ValueError) as exc:
        _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))
    try:
        outcome = asyncio.run(_play(task, policy, mcp, api, seed))
    except Exception as exc:
        _fail(f"cannot play against the server: {type(exc).__name__}: {exc}")
    line: dict[str, object] = {"steps": outcome.steps, "stop_reason": outcome.stop_reason}
    if api is not None:  # a sidecar grades only with its token, which the agent does not have
        line["score"] = outcome.score.total
    typer.echo(json.dumps(line))


export_app = typer.Typer(help="Export a task to another format.", no_args_is_help=True)
app.add_typer(export_app, name="export")


@export_app.command("harbor")
def export_harbor_command(
    task: Annotated[str, typer.Option("--task", help="Task ref, e.g. oncall@1.")],
    out: Annotated[Path, typer.Option(help="Output folder (a Harbor dataset folder).")],
    seeds: Annotated[str, typer.Option(help="Seeds, e.g. '0-9,12'.")] = "0-9",
    solution_policy: Annotated[
        str, typer.Option(help="Policy that solution/solve.sh plays (the oracle).")
    ] = "reference",
    wheel: Annotated[
        Path | None, typer.Option(help="Prebuilt engine wheel (default: build it).")
    ] = None,
    requirements: Annotated[
        Path | None, typer.Option(help="Pinned requirements for the wheel (with --wheel).")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace a non-empty --out.")] = False,
) -> None:
    """Write one Harbor task folder per seed."""
    import tempfile

    from crucible.export.harbor import (
        EngineArtifacts,
        ExportError,
        build_engine_artifacts,
        export_harbor,
    )

    if (wheel is None) != (requirements is None):
        _fail("give both --wheel and --requirements, or neither")
    try:
        seed_list = parse_seeds(seeds)
        get_task(task)
        get_policy(solution_policy, task)
    except (KeyError, ValueError) as exc:
        _fail(str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))
    with tempfile.TemporaryDirectory(prefix="crucible-wheel-") as tmp:
        try:
            if wheel is not None and requirements is not None:
                artifacts = EngineArtifacts(wheel=wheel, requirements=requirements)
            else:
                artifacts = build_engine_artifacts(Path(tmp))
            folders = export_harbor(
                task,
                seed_list,
                out,
                artifacts,
                solution_policy=solution_policy,
                force=force,
            )
        except (ExportError, OSError) as exc:
            _fail(str(exc))
    for folder in folders:
        typer.echo(folder)
    typer.echo(f"Build once: {out / 'build-sidecar.sh'}")
    typer.echo(f"Run: harbor run -p {out} -a oracle")


@app.command("new-env")
def new_env_command(
    name: Annotated[str, typer.Argument(help="Task id of the new environment, e.g. 'triage'.")],
    root: Annotated[
        Path | None, typer.Option(help="Engine folder to write into (default: this checkout).")
    ] = None,
) -> None:
    """Start a new environment as a copy of guess@1, with a reference policy and tests."""
    from crucible.scaffold import ScaffoldError, engine_root, new_env

    try:
        written = new_env(name, root if root is not None else engine_root())
    except (ScaffoldError, OSError) as exc:
        _fail(str(exc))
    for path in written:
        typer.echo(path)
    typer.echo(
        f"Registered {name}@1. Check it: uv run pytest tests/generic src/crucible/envs/{name}"
    )
    typer.echo(
        "Pin its scores: CRUCIBLE_UPDATE_GOLDEN=1 uv run pytest tests/policies/test_golden.py"
    )


@app.command("serve")
def serve_command() -> None:
    """Serve the HTTP API (settings come from the environment)."""
    import uvicorn

    from crucible.server.app import create_app
    from crucible.server.settings import Settings

    settings = Settings()
    uvicorn.run(
        create_app(settings), host="0.0.0.0", port=settings.port, log_level=settings.log_level
    )


@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option(help="Port for the workbench server.")] = 8000,
    host: Annotated[str, typer.Option(help="Host interface to bind.")] = "127.0.0.1",
    runs_dir: Annotated[Path, typer.Option(help="Directory for workbench episodes.")] = Path(
        "runs/server"
    ),
    jobs_dir: Annotated[Path, typer.Option(help="Harbor job output directory.")] = Path(
        "runs/harbor-jobs"
    ),
    exports_dir: Annotated[
        Path, typer.Option(help="Directory containing prepared Harbor exports.")
    ] = Path("runs"),
) -> None:
    """Serve manual play, Harbor evaluations, and saved Harbor runs."""
    import uvicorn

    from crucible.server.app import create_app
    from crucible.server.settings import Settings

    settings = Settings(
        mode="api",
        port=port,
        runs_dir=runs_dir,
        harbor_jobs_dir=jobs_dir,
        harbor_exports_dir=exports_dir,
    )
    typer.echo(f"Crucible Oncall Workbench -> http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level=settings.log_level)


__all__ = ["Row", "app", "summarise"]


@app.command("regrade")
def regrade_command(
    folder: Annotated[Path, typer.Argument(help="Harbor job folder or Crucible transcript.")],
    export: Annotated[
        Path | None, typer.Option(help="Original export containing world salts.")
    ] = None,
    salt_env: Annotated[
        str | None, typer.Option(help="Environment variable holding world salt.")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Write replay report here.")] = None,
) -> None:
    """Replay old actions with current scoring; flag changed observations."""
    import os

    from crucible.feedback.regrade import regrade_folder

    salt = os.environ.get(salt_env) if salt_env else None
    if salt_env and not salt:
        _fail(f"Missing environment variable {salt_env}")
    try:
        rows = asyncio.run(regrade_folder(folder, export=export, salt=salt))
    except (ValueError, OSError) as exc:
        _fail(str(exc))
    report = json.dumps(rows, indent=2) + "\n"
    if out is not None:
        out.write_text(report)
    typer.echo(report)
    if any(not row["comparable"] for row in rows):
        raise typer.Exit(1)


@app.command("gates")
def gates_command(
    out: Annotated[Path, typer.Option(help="New measurement directory.")],
    seeds: Annotated[str, typer.Option()] = "0-49",
    salt_env: Annotated[str, typer.Option()] = "CRUCIBLE_EVAL_SALT",
) -> None:
    """Measure the oncall bot gates on identical salted worlds."""
    import os

    from crucible.feedback.gates import measure

    salt = os.environ.get(salt_env)
    if not salt:
        _fail(f"Set {salt_env}")
    try:
        result = asyncio.run(measure(parse_seeds(seeds), salt, out))
    except (ValueError, KeyError, OSError) as exc:
        _fail(str(exc))
    typer.echo(json.dumps(result, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)


@app.command("compare")
def compare_command(
    bots: Annotated[Path, typer.Option(help="Salted bot gate output directory.")],
    agent: Annotated[Path, typer.Option(help="Historical model screening output directory.")],
) -> None:
    """Compare archived screening results; new model evaluations run through Harbor."""
    from crucible.feedback.compare import compare

    try:
        result = compare(bots, agent)
    except (ValueError, KeyError, OSError) as exc:
        _fail(str(exc))
    typer.echo(json.dumps(result, indent=2))
    if not result["passed"]:
        raise typer.Exit(1)
