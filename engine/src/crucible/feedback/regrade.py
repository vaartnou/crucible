"""Replay recorded actions, reporting the first observation divergence explicitly."""

import asyncio
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from crucible.core.registry import get_task
from crucible.core.transcript import Footer, Header, StepRecord, read_transcript
from crucible.feedback.observations import observations_match


def _records(path: Path) -> tuple[Header, list[StepRecord], Footer]:
    if path.suffix == ".jsonl":
        transcript = read_transcript(path)
        return transcript.header, transcript.steps, transcript.footer
    raw = TypeAdapter(list[dict[str, Any]]).validate_json(path.read_text())
    if len(raw) < 2:
        raise ValueError(f"Not a Crucible sidecar transcript: {path}")
    return (
        Header.model_validate(raw[0]),
        [StepRecord.model_validate(row) for row in raw[1:-1]],
        Footer.model_validate(raw[-1]),
    )


def _salt_for(header: Header, export: Path | None, salt: str | None) -> str | None:
    if not header.salted:
        return None
    if salt is not None:
        return salt
    if export is None:
        raise ValueError("Salted transcript requires --export or --salt-env")
    candidates: list[str] = []
    for compose in export.glob("*/environment/docker-compose.yaml"):
        data: Any = yaml.safe_load(compose.read_text())
        env = data["services"]["mcp-server"]["environment"]
        if env["SIDECAR_TASK"] == header.task and int(env["SIDECAR_SEED"]) == header.seed:
            candidates.append(str(env["SIDECAR_SALT"]))
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one exported world for {header.task} seed {header.seed}"
        )
    return candidates[0]


async def regrade(
    path: Path, *, export: Path | None = None, salt: str | None = None, numeric_tolerance: float = 0
) -> dict[str, object]:
    header, steps, footer = _records(path)
    task = get_task(header.task)
    divergence: int | None = None
    exact_divergence: int | None = None
    try:
        await task.reset(header.seed, salt=_salt_for(header, export, salt))
        for index, step in enumerate(steps):
            if task.done() or index >= task.spec.max_steps:
                divergence = index if divergence is None else divergence
                break
            result = await task.step(step.call)
            if result != step.result and exact_divergence is None:
                exact_divergence = index
            if (
                not observations_match(result, step.result, numeric_tolerance)
                and divergence is None
            ):
                divergence = index
        score = await task.grade()
    finally:
        await task.aclose()
    return {
        "transcript": str(path),
        "task": header.task,
        "seed": header.seed,
        "old_reward": footer.score.total,
        "reward": score.total,
        "breakdown": score.breakdown,
        "first_divergence": divergence,
        "first_exact_divergence": exact_divergence,
        "numeric_tolerance": numeric_tolerance,
        "comparable": divergence is None,
        "old_scoring_hash": header.scoring_hash,
        "scoring_hash": task.scoring_hash,
    }


async def regrade_folder(
    folder: Path,
    *,
    export: Path | None = None,
    salt: str | None = None,
    numeric_tolerance: float = 0,
) -> list[dict[str, object]]:
    paths = await asyncio.to_thread(_paths, folder)
    if not paths:
        raise ValueError("No transcripts found")
    rows: list[dict[str, object]] = []
    for path in paths:
        try:
            rows.append(
                await regrade(path, export=export, salt=salt, numeric_tolerance=numeric_tolerance)
            )
        except (ValueError, KeyError, OSError) as exc:
            rows.append({"transcript": str(path), "error": str(exc), "comparable": False})
    return rows


def _paths(folder: Path) -> list[Path]:
    if folder.is_file():
        return [folder]
    return sorted(set(folder.rglob("transcript.json")) | set(folder.rglob("*.jsonl")))
