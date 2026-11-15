"""Discover and replay saved transcripts, live `.jsonl.tmp` runs, and Harbor containers."""

import asyncio
import json
import os
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import TypeAdapter

from crucible.core.jobs import Job
from crucible.core.registry import get_policy, get_task
from crucible.core.scoring import Score
from crucible.core.tools import Observation, result_text
from crucible.core.transcript import Footer, Header, StepRecord
from crucible.server.inspector import build_fog_state, build_god_state

_HEADER_ADAPTER = TypeAdapter(Header)
_STEP_ADAPTER = TypeAdapter(StepRecord)
_FOOTER_ADAPTER = TypeAdapter(Footer)
_LIST_DICT_ADAPTER = TypeAdapter(list[dict[str, Any]])

_BASELINE_EPISODE_CACHE: OrderedDict[
    tuple[str, int, int, str | None, str],
    tuple[list[StepRecord], Score, str],
] = OrderedDict()
_CACHE_LIMIT = 64


def _as_dict(val: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], val) if isinstance(val, dict) else None


def _as_list(val: object) -> list[Any]:
    return cast(list[Any], val) if isinstance(val, list) else []


class ParsedTranscript:
    """Header, partial or complete steps, and optional footer (None while live)."""

    def __init__(
        self,
        header: Header,
        steps: list[StepRecord],
        footer: Footer | None,
        *,
        live: bool,
        source_kind: str,
        path: str,
        grade_extra: dict[str, Any] | None = None,
        agent_logs: str | None = None,
    ) -> None:
        self.header = header
        self.steps = steps
        self.footer = footer
        self.live = live
        self.source_kind = source_kind
        self.path = path
        self.grade_extra = grade_extra or {}
        self.agent_logs = agent_logs


def parse_jsonl_lines(
    text: str, *, path_label: str, force_live: bool = False
) -> ParsedTranscript | None:
    """Parse a finished `.jsonl` or an in-progress `.jsonl.tmp` without requiring a footer."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        header = _HEADER_ADAPTER.validate_json(lines[0])
    except ValueError:
        return None
    steps: list[StepRecord] = []
    footer: Footer | None = None
    for raw in lines[1:]:
        try:
            loaded: object = json.loads(raw)
        except ValueError:
            break
        obj = _as_dict(loaded)
        if obj is None:
            continue
        rec_type = obj.get("type")
        try:
            if rec_type == "step":
                steps.append(_STEP_ADAPTER.validate_python(obj))
            elif rec_type == "footer":
                footer = _FOOTER_ADAPTER.validate_python(obj)
        except ValueError:
            break
    return ParsedTranscript(
        header=header,
        steps=steps,
        footer=footer,
        live=force_live or footer is None,
        source_kind="jsonl",
        path=path_label,
    )


def _parse_harbor_transcript_file(path: Path) -> ParsedTranscript | None:
    try:
        raw = _LIST_DICT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if len(raw) < 2:
        return None
    try:
        header = Header.model_validate(raw[0])
        steps = [StepRecord.model_validate(r) for r in raw[1:-1]]
        footer = Footer.model_validate(raw[-1])
    except ValueError:
        return None

    grade_extra: dict[str, Any] = {}
    grade_file = path.with_name("grade.json")
    if grade_file.is_file():
        try:
            loaded_g: object = json.loads(grade_file.read_text(encoding="utf-8"))
            as_g = _as_dict(loaded_g)
            if as_g is not None:
                grade_extra = as_g
        except OSError, ValueError:
            pass

    agent_logs = _read_sibling_agent_logs(path.parent.parent / "agent")
    return ParsedTranscript(
        header=header,
        steps=steps,
        footer=footer,
        live=False,
        source_kind="harbor",
        path=str(path),
        grade_extra=grade_extra,
        agent_logs=agent_logs,
    )


def _read_sibling_agent_logs(agent_dir: Path) -> str | None:
    if not agent_dir.is_dir():
        return None
    snippets: list[str] = []
    try:
        candidates = sorted(
            (p for p in agent_dir.iterdir() if p.is_file() and p.stat().st_size > 0),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for fpath in candidates[:3]:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tail = "\n".join(content.splitlines()[-40:])
        if tail.strip():
            snippets.append(f"=== {fpath.name} ===\n{tail}")
    return "\n\n".join(snippets) if snippets else None


def _search_roots(extra_dirs: Sequence[Path]) -> list[Path]:
    cwd = Path.cwd()
    candidates = [
        *extra_dirs,
        cwd / "runs",
        cwd.parent / "runs",
        cwd / "jobs",
        cwd.parent / "jobs",
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _index_export_salts(roots: Sequence[Path]) -> dict[tuple[str, int], list[tuple[str, str]]]:
    """Map `(task_ref, seed)` -> list of `(export_folder_name, salt)` found in Harbor exports."""
    mapping: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for root in roots:
        for compose in root.glob("*/*/environment/docker-compose.yaml"):
            try:
                data: Any = yaml.safe_load(compose.read_text(encoding="utf-8"))
                env = data["services"]["mcp-server"]["environment"]
                task_ref = str(env["SIDECAR_TASK"])
                seed = int(env["SIDECAR_SEED"])
                salt = str(env["SIDECAR_SALT"])
                export_dir = str(compose.parents[2])
                mapping.setdefault((task_ref, seed), []).append((export_dir, salt))
            except OSError, KeyError, TypeError, ValueError, yaml.YAMLError:
                continue
    return mapping


def _scan_runs_sync(extra_dirs: Sequence[Path]) -> list[dict[str, Any]]:
    roots = _search_roots(extra_dirs)
    items: list[tuple[float, dict[str, Any]]] = []
    seen_files: set[Path] = set()

    for root in roots:
        for pattern in ("**/*.jsonl.tmp", "**/*.jsonl", "**/verifier/transcript.json"):
            for fpath in root.glob(pattern):
                try:
                    resolved = fpath.resolve()
                    stat = resolved.stat()
                except OSError:
                    continue
                if resolved in seen_files:
                    continue
                if resolved.name.endswith(".jsonl.tmp"):
                    final_p = resolved.with_name(resolved.name.removesuffix(".tmp"))
                    if final_p.exists():
                        continue
                seen_files.add(resolved)

                parsed: ParsedTranscript | None
                if resolved.name == "transcript.json":
                    parsed = _parse_harbor_transcript_file(resolved)
                    group = resolved.parents[2].name if len(resolved.parents) >= 3 else root.name
                else:
                    try:
                        raw_txt = resolved.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    parsed = parse_jsonl_lines(
                        raw_txt,
                        path_label=str(resolved),
                        force_live=resolved.name.endswith(".tmp"),
                    )
                    group = resolved.parent.name

                if parsed is None:
                    continue

                score_total: float | None = (
                    round(parsed.footer.score.total, 4) if parsed.footer is not None else None
                )
                flags: dict[str, bool] = (
                    dict(parsed.footer.score.flags) if parsed.footer is not None else {}
                )
                breakdown: dict[str, float] = (
                    {k: round(v, 4) for k, v in parsed.footer.score.breakdown.items()}
                    if parsed.footer is not None
                    else {}
                )
                stop_reason: str | None = (
                    parsed.footer.stop_reason if parsed.footer is not None else None
                )
                items.append(
                    (
                        stat.st_mtime,
                        {
                            "id": str(resolved),
                            "path": str(resolved),
                            "filename": resolved.name,
                            "group": group,
                            "source_kind": parsed.source_kind,
                            "live": parsed.live,
                            "task": parsed.header.task,
                            "policy": parsed.header.policy,
                            "seed": parsed.header.seed,
                            "salted": parsed.header.salted,
                            "steps": len(parsed.steps),
                            "score": score_total,
                            "breakdown": breakdown,
                            "flags": flags,
                            "stop_reason": stop_reason,
                            "started_at": parsed.header.started_at,
                            "ended_at": parsed.footer.ended_at if parsed.footer else None,
                            "mtime": stat.st_mtime,
                        },
                    )
                )

    items.sort(key=lambda pair: (not bool(pair[1]["live"]), -pair[0]))
    return [row for _, row in items[:250]]


async def list_saved_runs(extra_dirs: Sequence[Path]) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_scan_runs_sync, extra_dirs)


def _load_parsed_sync(path: Path) -> ParsedTranscript:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Transcript not found: {path}")
    if resolved.name == "transcript.json":
        parsed = _parse_harbor_transcript_file(resolved)
    else:
        parsed = parse_jsonl_lines(
            resolved.read_text(encoding="utf-8"),
            path_label=str(resolved),
            force_live=resolved.name.endswith(".tmp"),
        )
    if parsed is None:
        raise ValueError(f"Unreadable or empty transcript: {path}")
    return parsed


def _candidate_salts(
    header: Header, extra_dirs: Sequence[Path], explicit_export: Path | None = None
) -> list[tuple[str, str | None]]:
    if not header.salted:
        return [("unsalted", None)]
    candidates: list[tuple[str, str | None]] = []
    if explicit_export is not None:
        indexed = _index_export_salts([explicit_export.parent, explicit_export])
        for exp_dir, salt in indexed.get((header.task, header.seed), []):
            candidates.append((f"export:{Path(exp_dir).name}", salt))
    env_salt = os.environ.get("CRUCIBLE_EVAL_SALT")
    if env_salt:
        candidates.append(("env:CRUCIBLE_EVAL_SALT", env_salt))
    roots = _search_roots(extra_dirs)
    for exp_dir, salt in _index_export_salts(roots).get((header.task, header.seed), []):
        item = (f"export:{Path(exp_dir).name}", salt)
        if item not in candidates:
            candidates.append(item)
    return candidates


def serialize_steps(steps: Sequence[StepRecord]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev_minute = 5
    for rec in steps:
        content = _as_dict(rec.result.content)
        minute_val = content.get("minute") if content is not None else None
        obs_val = content.get("observed_at") if content is not None else None
        minute_after = minute_val if isinstance(minute_val, int) else prev_minute
        obs_at = obs_val if isinstance(obs_val, int) else prev_minute
        cost = max(0, minute_after - obs_at)
        prev_minute = minute_after
        out.append(
            {
                "step": rec.step,
                "call": rec.call.model_dump(mode="json"),
                "result": rec.result.model_dump(mode="json"),
                "observed_at": obs_at,
                "minute_after": minute_after,
                "cost_minutes": cost,
            }
        )
    return out


async def replay_parsed_transcript(
    parsed: ParsedTranscript,
    *,
    extra_dirs: Sequence[Path],
    explicit_salt: str | None = None,
    explicit_export: Path | None = None,
    up_to_step: int | None = None,
    inspect_minute: int | None = None,
    reveal_secret: bool = False,
) -> dict[str, Any]:
    """Replay `parsed` up to `up_to_step` and return Fog-of-War plus God-View."""
    total_steps = len(parsed.steps)
    limit = total_steps if up_to_step is None else max(0, min(total_steps, up_to_step))
    sliced_steps = parsed.steps[:limit]
    fog = build_fog_state(sliced_steps)

    salt_candidates: list[tuple[str, str | None]]
    if explicit_salt is not None:
        salt_candidates = [("container_env", explicit_salt)]
    else:
        salt_candidates = await asyncio.to_thread(
            _candidate_salts, parsed.header, extra_dirs, explicit_export
        )

    matched_god: dict[str, Any] | None = None
    salt_status = "missing_salt" if parsed.header.salted and not salt_candidates else "unverified"
    matched_salt_value: str | None = None

    for source_label, candidate_salt in salt_candidates:
        task = get_task(parsed.header.task)
        diverged = False
        try:
            await task.reset(parsed.header.seed, salt=candidate_salt)
            for rec in sliced_steps:
                if task.done():
                    break
                res = await task.step(rec.call)
                if res != rec.result:
                    diverged = True
            god = build_god_state(task, inspect_minute=inspect_minute, reveal_secret=reveal_secret)
        finally:
            await task.aclose()
        if not diverged:
            matched_god = god
            salt_status = f"matched ({source_label})"
            matched_salt_value = candidate_salt
            break
        if matched_god is None and not parsed.header.salted:
            matched_god = god
            salt_status = "diverged_simulator"

    return {
        "path": parsed.path,
        "source_kind": parsed.source_kind,
        "live": parsed.live,
        "header": parsed.header.model_dump(mode="json"),
        "footer": parsed.footer.model_dump(mode="json") if parsed.footer else None,
        "total_steps": total_steps,
        "selected_step": limit,
        "steps": serialize_steps(sliced_steps),
        "all_steps": serialize_steps(parsed.steps),
        "fog_state": fog,
        "god_state": matched_god,
        "salt_status": salt_status,
        "has_matched_world": matched_god is not None,
        "agent_logs": parsed.agent_logs,
        "_matched_salt": matched_salt_value,
    }


async def load_and_replay_file(
    path: Path,
    *,
    extra_dirs: Sequence[Path],
    explicit_export: Path | None = None,
    up_to_step: int | None = None,
    inspect_minute: int | None = None,
    reveal_secret: bool = False,
) -> dict[str, Any]:
    parsed = await asyncio.to_thread(_load_parsed_sync, path)
    payload = await replay_parsed_transcript(
        parsed,
        extra_dirs=extra_dirs,
        explicit_export=explicit_export,
        up_to_step=up_to_step,
        inspect_minute=inspect_minute,
        reveal_secret=reveal_secret,
    )
    payload.pop("_matched_salt", None)
    return payload


async def _run_cmd(args: list[str], limit_sec: float = 5.0) -> tuple[int, str, str]:
    try:
        async with asyncio.timeout(limit_sec):
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
    except OSError, TimeoutError:
        return 1, "", "command unavailable or timed out"
    return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")


def _env_map(env_list: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in env_list:
        if isinstance(entry, str) and "=" in entry:
            k, v = entry.split("=", 1)
            result[k] = v
    return result


async def list_harbor_containers() -> dict[str, Any]:
    """Discover running Harbor `mcp-server` containers (`crucible-sidecar:*`) and sibling agents."""
    rc, out, err = await _run_cmd(["docker", "ps", "--format", "{{json .}}"])
    if rc != 0:
        return {
            "docker_available": False,
            "error": err.strip() or "docker ps failed",
            "containers": [],
        }

    rows: list[dict[str, Any]] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            loaded_row: object = json.loads(ln)
            d_row = _as_dict(loaded_row)
            if d_row is not None:
                rows.append(d_row)
        except ValueError:
            continue

    if not rows:
        return {"docker_available": True, "error": None, "containers": []}

    ids = [str(r["ID"]) for r in rows if "ID" in r]
    rc_i, out_i, _ = await _run_cmd(["docker", "inspect", *ids])
    if rc_i != 0:
        return {"docker_available": True, "error": "docker inspect failed", "containers": []}

    try:
        inspected_raw: object = json.loads(out_i)
    except ValueError:
        return {
            "docker_available": True,
            "error": "invalid docker inspect output",
            "containers": [],
        }
    if not isinstance(inspected_raw, list):
        return {"docker_available": True, "error": None, "containers": []}

    main_by_project: dict[str, dict[str, str]] = {}
    sidecars: list[dict[str, Any]] = []

    for raw_item in cast(list[object], inspected_raw):
        item = _as_dict(raw_item)
        if item is None:
            continue
        cfg = _as_dict(item.get("Config")) or {}
        labels = _as_dict(cfg.get("Labels")) or {}
        env_raw = _as_list(cfg.get("Env"))
        project = str(labels.get("com.docker.compose.project", ""))
        service = str(labels.get("com.docker.compose.service", ""))
        image = str(cfg.get("Image", ""))
        cid = str(item.get("Id", ""))[:12]
        cname = str(item.get("Name", "")).lstrip("/")

        if service == "main" and project:
            main_by_project[project] = {"id": cid, "name": cname, "image": image}
        elif (
            service == "mcp-server"
            or image.startswith("crucible-sidecar")
            or "MODE=sidecar" in env_raw
        ):
            sidecars.append(item)

    results: list[dict[str, Any]] = []
    for item in sidecars:
        cfg = _as_dict(item.get("Config")) or {}
        labels = _as_dict(cfg.get("Labels")) or {}
        state_obj = _as_dict(item.get("State")) or {}
        env = _env_map(_as_list(cfg.get("Env")))
        cid = str(item.get("Id", ""))[:12]
        cname = str(item.get("Name", "")).lstrip("/")
        project = str(labels.get("com.docker.compose.project", ""))
        workdir = str(labels.get("com.docker.compose.project.working_dir", ""))
        runs_dir = env.get("RUNS_DIR", "/tmp/runs")
        task_ref = env.get("SIDECAR_TASK", "oncall@1")
        try:
            seed = int(env.get("SIDECAR_SEED", "0"))
        except ValueError:
            seed = 0

        rc_e, out_e, _ = await _run_cmd(
            [
                "docker",
                "exec",
                cid,
                "sh",
                "-c",
                f"cat {runs_dir}/episodes/*.jsonl* 2>/dev/null || true",
            ],
            limit_sec=3.0,
        )
        parsed = (
            parse_jsonl_lines(out_e, path_label=f"docker://{cid}", force_live=True)
            if rc_e == 0
            else None
        )
        health_obj = _as_dict(state_obj.get("Health")) or {}
        sibling = main_by_project.get(project)

        results.append(
            {
                "container_id": cid,
                "container_name": cname,
                "image": str(cfg.get("Image", "")),
                "compose_project": project or cname,
                "working_dir": workdir,
                "task": task_ref,
                "seed": seed,
                "salted": bool(env.get("SIDECAR_SALT")),
                "status": str(state_obj.get("Status", "running")),
                "health": str(health_obj.get("Status", "unknown")),
                "started_at": str(state_obj.get("StartedAt", "")),
                "steps": len(parsed.steps) if parsed is not None else 0,
                "episode_closed": bool(parsed and parsed.footer is not None),
                "agent_container": sibling,
            }
        )

    return {"docker_available": True, "error": None, "containers": results}


async def tail_harbor_container(
    container_id: str,
    *,
    up_to_step: int | None = None,
    inspect_minute: int | None = None,
    reveal_secret: bool = False,
) -> dict[str, Any]:
    """Read live `.jsonl(.tmp)` from a running Harbor `mcp-server` container."""
    if not container_id.isalnum():
        raise ValueError("Invalid container ID")
    rc_i, out_i, err_i = await _run_cmd(["docker", "inspect", container_id])
    if rc_i != 0:
        raise ValueError(f"Container {container_id} not found: {err_i.strip()}")
    inspected_raw: object = json.loads(out_i)
    if not isinstance(inspected_raw, list) or not inspected_raw:
        raise ValueError(f"Empty inspect output for {container_id}")

    item = _as_dict(cast(list[object], inspected_raw)[0]) or {}
    cfg = _as_dict(item.get("Config")) or {}
    labels = _as_dict(cfg.get("Labels")) or {}
    env = _env_map(_as_list(cfg.get("Env")))
    runs_dir = env.get("RUNS_DIR", "/tmp/runs")
    salt = env.get("SIDECAR_SALT") or None
    task_ref = env.get("SIDECAR_TASK", "oncall@1")
    seed = int(env.get("SIDECAR_SEED", "0"))
    project = str(labels.get("com.docker.compose.project", ""))

    rc_e, out_e, _ = await _run_cmd(
        [
            "docker",
            "exec",
            container_id,
            "sh",
            "-c",
            f"cat {runs_dir}/episodes/*.jsonl* 2>/dev/null || true",
        ],
        limit_sec=4.0,
    )
    parsed = parse_jsonl_lines(out_e, path_label=f"docker://{container_id}") if rc_e == 0 else None
    if parsed is None:
        dummy_header = Header(
            task=task_ref,
            scoring_hash="live",
            policy=project or "harbor-agent",
            seed=seed,
            repeat=0,
            job_id=container_id,
            git_commit="live",
            started_at="",
            salted=salt is not None,
        )
        parsed = ParsedTranscript(
            header=dummy_header,
            steps=[],
            footer=None,
            live=True,
            source_kind="harbor_live",
            path=f"docker://{container_id}",
        )
    else:
        parsed.source_kind = "harbor_live"

    if project:
        rc_ps, out_ps, _ = await _run_cmd(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=main",
                "--format",
                "{{.ID}}",
            ]
        )
        main_id = out_ps.strip().splitlines()[0] if rc_ps == 0 and out_ps.strip() else ""
        if main_id and main_id.isalnum():
            _, agent_out, _ = await _run_cmd(
                [
                    "docker",
                    "exec",
                    main_id,
                    "sh",
                    "-c",
                    "head -c 16384 /logs/agent/* 2>/dev/null | tail -n 60 || true",
                ],
                limit_sec=3.0,
            )
            if agent_out.strip():
                parsed.agent_logs = agent_out

    payload = await replay_parsed_transcript(
        parsed,
        extra_dirs=[],
        explicit_salt=salt,
        up_to_step=up_to_step,
        inspect_minute=inspect_minute,
        reveal_secret=reveal_secret,
    )
    payload.pop("_matched_salt", None)
    payload["compose_project"] = project
    payload["container_id"] = container_id
    return payload


async def run_baseline_comparison(
    task_ref: str,
    seed: int,
    *,
    difficulty: int = 0,
    policy_name: str = "graph_walk",
    salt: str | None = None,
    up_to_step: int | None = None,
    inspect_minute: int | None = None,
    reveal_secret: bool = False,
) -> dict[str, Any]:
    """Run a public policy (e.g. `graph_walk` or `reference`) on the identical world for compare."""
    cache_key = (task_ref, seed, difficulty, salt, policy_name)
    cached = _BASELINE_EPISODE_CACHE.get(cache_key)
    if cached is not None:
        _BASELINE_EPISODE_CACHE.move_to_end(cache_key)
        steps, score, stop_reason = cached
    else:
        task = get_task(task_ref)
        policy = get_policy(policy_name, task_ref)
        steps: list[StepRecord] = []
        try:
            obs = await task.reset(seed, difficulty=difficulty, salt=salt)
            await policy.reset(task.spec, task.tools(), task.instruction())
            for step_idx in range(task.spec.max_steps):
                if task.done():
                    break
                call = await policy.act(obs)
                if call is None:
                    break
                res = await task.step(call)
                steps.append(StepRecord(step=step_idx, call=call, result=res))
                obs = Observation(text=result_text(res), last_result=res)
            stop_reason = (
                "task_done"
                if task.done()
                else "budget_exhausted"
                if len(steps) >= task.spec.max_steps
                else "policy_stop"
            )
            score = await task.grade()
        finally:
            await task.aclose()
        _BASELINE_EPISODE_CACHE[cache_key] = (steps, score, stop_reason)
        if len(_BASELINE_EPISODE_CACHE) > _CACHE_LIMIT:
            _BASELINE_EPISODE_CACHE.popitem(last=False)

    total_steps = len(steps)
    limit = total_steps if up_to_step is None else max(0, min(total_steps, up_to_step))
    sliced = steps[:limit]

    task = get_task(task_ref)
    try:
        await task.reset(seed, difficulty=difficulty, salt=salt)
        for rec in sliced:
            if task.done():
                break
            await task.step(rec.call)
        god = build_god_state(task, inspect_minute=inspect_minute, reveal_secret=reveal_secret)
    finally:
        await task.aclose()

    job = Job(task=task_ref, policy=policy_name, seed=seed)
    return {
        "policy": policy_name,
        "task": task_ref,
        "seed": seed,
        "difficulty": difficulty,
        "job_id": job.job_id,
        "total_steps": total_steps,
        "selected_step": limit,
        "steps": serialize_steps(sliced),
        "all_steps": serialize_steps(steps),
        "score": score.model_dump(mode="json"),
        "stop_reason": stop_reason,
        "fog_state": build_fog_state(sliced),
        "god_state": god,
    }


__all__ = [
    "ParsedTranscript",
    "list_harbor_containers",
    "list_saved_runs",
    "load_and_replay_file",
    "parse_jsonl_lines",
    "replay_parsed_transcript",
    "run_baseline_comparison",
    "serialize_steps",
    "tail_harbor_container",
]
