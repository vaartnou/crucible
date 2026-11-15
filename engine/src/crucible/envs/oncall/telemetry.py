"""Public views accept snapshots, never private world faults."""

from collections import defaultdict
from dataclasses import asdict
from typing import Literal

from pydantic import JsonValue

from crucible.envs.oncall.model import EdgeSample, PodSample

Metric = Literal[
    "error_ratio", "p99_latency", "request_rate", "memory", "pool_in_use", "queue_depth"
]


def metrics(
    history: list[tuple[PodSample, ...]],
    metric: Metric,
    service: str,
    by: str,
    window: int,
    end_minute: int | None = None,
    series: bool = False,
) -> list[dict[str, JsonValue]]:
    """Aggregate recorded samples, optionally retaining the minute dimension."""
    end = history[-1][0].minute if end_minute is None else end_minute
    per_minute: dict[tuple[str, int], list[float]] = defaultdict(list)
    for frame in history:
        if not end - window < frame[0].minute <= end:
            continue
        for sample in frame:
            if service == "*" or sample.service == service:
                name = sample.pod if by == "pod" else sample.service
                per_minute[(name, sample.minute)].append(float(getattr(sample, metric)))
    groups: dict[tuple[str, int | None], list[float]] = defaultdict(list)
    for (name, minute), values in per_minute.items():
        if by == "service" and metric in ("request_rate", "pool_in_use", "queue_depth"):
            aggregate = sum(values)
        elif metric == "p99_latency":
            aggregate = max(values)
        else:
            aggregate = sum(values) / len(values)
        groups[(name, minute if series else None)].append(aggregate)
    result: list[dict[str, JsonValue]] = []
    for (name, minute), values in groups.items():
        row: dict[str, JsonValue] = {"name": name, "value": sum(values) / len(values)}
        if minute is not None:
            row["minute"] = minute
        result.append(row)
    return result


def edges(
    history: list[tuple[EdgeSample, ...]],
    service: str,
    window: int,
    end_minute: int | None = None,
    series: bool = False,
) -> list[dict[str, JsonValue]]:
    """Recorded edge samples, aggregated or returned at their original minute."""
    end = history[-1][0].minute if end_minute is None else end_minute
    selected = [
        edge
        for frame in history
        if end - window < frame[0].minute <= end
        for edge in frame
        if service == "*" or edge.caller == service
    ]
    if series:
        return [asdict(edge) for edge in selected]
    grouped: dict[tuple[str, str], list[EdgeSample]] = defaultdict(list)
    for edge in selected:
        grouped[(edge.caller, edge.callee)].append(edge)
    result: list[dict[str, JsonValue]] = []
    fields = (
        "request_rate",
        "attempt_rate",
        "error_ratio",
        "latency",
        "concurrency",
        "pool_in_use",
        "timeout_ms",
        "pool_size",
        "retries",
        "offered_attempt_rate",
        "pool_rejection_ratio",
    )
    for (caller, callee), samples in grouped.items():
        row: dict[str, JsonValue] = {"caller": caller, "callee": callee}
        for field in fields:
            row[field] = sum(float(getattr(sample, field)) for sample in samples) / len(samples)
        row["solver_residual"] = max(sample.solver_residual for sample in samples)
        row["solver_iterations"] = max(sample.solver_iterations for sample in samples)
        result.append(row)
    return result


def logs(
    history: list[tuple[PodSample, ...]],
    service: str,
    query: str,
    level: str,
    since: int,
    page: int,
) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    now = history[-1][0].minute
    for frame in reversed(history):
        for row in frame:
            severity = "ERROR" if row.error_ratio > 0.01 else "INFO"
            if (
                row.service == service
                and row.minute >= now - since
                and query.lower() in row.message.lower()
                and (not level or severity == level)
            ):
                rows.append(
                    {
                        "minute": row.minute,
                        "pod": row.pod,
                        "level": severity,
                        "message": row.message,
                    }
                )
    return rows[page * 20 : (page + 1) * 20]


def _error_onset(history: list[tuple[PodSample, ...]], service: str) -> int:
    """Start of the current contiguous error episode, using recorded samples only."""
    onset = history[-1][0].minute
    for frame in reversed(history):
        values = [row.error_ratio for row in frame if row.service == service]
        if not values or sum(values) / len(values) <= 0.05:
            break
        onset = frame[0].minute
    return onset


def alerts(history: list[tuple[PodSample, ...]]) -> list[dict[str, JsonValue]]:
    rows = metrics(history, "error_ratio", "*", "service", 5)
    result: list[dict[str, JsonValue]] = []
    for row in rows:
        value = row["value"]
        if isinstance(value, float) and value > 0.05:
            name = str(row["name"])
            result.append(
                {
                    "id": f"errors-{name}",
                    "service": name,
                    "severity": 3 if name == "edge-gateway" else 2,
                    "error_ratio": value,
                    "started_at": _error_onset(history, name),
                }
            )
    result.append(
        {
            "id": "routine-cache",
            "service": "session-cache",
            "severity": 1,
            "error_ratio": 0.0,
            "started_at": -90,
        }
    )
    return sorted(
        result, key=lambda r: (-int(str(r["severity"])), int(str(r["started_at"])), str(r["id"]))
    )


def pod_table(frame: tuple[PodSample, ...]) -> str:
    return "NAME READY STATUS RESTARTS VERSION\n" + "\n".join(
        f"{r.pod} 1/1 {r.state} {int(r.state != 'Running')} {r.version}" for r in frame
    )
