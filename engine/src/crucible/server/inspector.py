"""Read-only cluster state, Fog-of-War, and call-path waterfall extraction for the UI."""

import json
import shlex
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, cast

from crucible.core.task import Task
from crucible.core.transcript import StepRecord
from crucible.envs.oncall import telemetry
from crucible.envs.oncall.grader import loss
from crucible.envs.oncall.model import (
    EdgeSample,
    PodSample,
    State,
    advance,
    effective_parameters,
)
from crucible.envs.oncall.task import OncallTask
from crucible.envs.oncall.world import HORIZON, Service, World

_BASELINE_SERIES_CACHE: OrderedDict[tuple[str, int], list[float]] = OrderedDict()
_CACHE_LIMIT = 128


def _as_dict(val: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], val) if isinstance(val, dict) else None


def _oncall_internals(task: OncallTask) -> tuple[World | None, State]:
    return (
        task._world,  # pyright: ignore[reportPrivateUsage]
        task._state,  # pyright: ignore[reportPrivateUsage]
    )


def _baseline_series(world: World, end_minute: int) -> list[float]:
    horizon = max(end_minute, 60)
    key = (
        json.dumps(asdict(world), sort_keys=True, separators=(",", ":"), allow_nan=False),
        horizon,
    )
    cached = _BASELINE_SERIES_CACHE.get(key)
    if cached is not None:
        _BASELINE_SERIES_CACHE.move_to_end(key)
        return cached[: end_minute + 31]
    probe = State(minute=-31)
    advance(world, probe, horizon + 31)
    series = [round(loss(frame), 4) for frame in probe.history]
    _BASELINE_SERIES_CACHE[key] = series
    if len(_BASELINE_SERIES_CACHE) > _CACHE_LIMIT:
        _BASELINE_SERIES_CACHE.popitem(last=False)
    return series[: end_minute + 31]


def _service_from_pod(pod_name: str) -> str:
    parts = pod_name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod_name


def build_fog_state(steps: Sequence[StepRecord]) -> dict[str, Any]:
    """Reconstruct everything an agent has discovered strictly from public tool results."""
    current_minute = 5
    topology_known = False
    services: dict[str, dict[str, Any]] = {}
    pods: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    alerts: list[dict[str, Any]] = []
    changes: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    series_map: dict[str, list[dict[str, Any]]] = {}
    actions_taken: list[dict[str, Any]] = []
    reverted_ids: set[str] = set()

    def ensure_service(name: str) -> dict[str, Any]:
        if name not in services:
            services[name] = {
                "name": name,
                "kind": None,
                "dependencies": None,
                "metrics": {},
                "observed_at": None,
            }
        return services[name]

    for record in steps:
        call, result = record.call, record.result
        content = _as_dict(result.content)
        if not result.ok or content is None:
            continue
        minute_val = content.get("minute")
        obs_val = content.get("observed_at")
        if isinstance(minute_val, int):
            current_minute = minute_val
        observed_at = obs_val if isinstance(obs_val, int) else current_minute
        data = content.get("data")

        if call.name == "run":
            cmd = str(call.args.get("command", ""))
            try:
                words = shlex.split(cmd)
            except ValueError:
                words = []
            if words == ["kubectl", "get", "services"] and isinstance(data, list):
                topology_known = True
                for raw_item in cast(list[object], data):
                    item = _as_dict(raw_item)
                    if item is not None and isinstance(item.get("name"), str):
                        svc = ensure_service(str(item["name"]))
                        svc["kind"] = item.get("kind")
                        svc["dependencies"] = item.get("dependencies")
                        svc["observed_at"] = observed_at
            elif words[:3] == ["kubectl", "get", "pods"] and isinstance(data, str):
                lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
                for ln in lines[1:]:
                    cols = ln.split()
                    if len(cols) >= 5:
                        pod_name, ready, status, restarts_s, version = cols[:5]
                        svc_name = _service_from_pod(pod_name)
                        ensure_service(svc_name)
                        default_pod: dict[str, Any] = {
                            "pod": pod_name,
                            "service": svc_name,
                            "metrics": {},
                        }
                        existing = pods.get(pod_name, default_pod)
                        existing.update(
                            {
                                "ready": ready,
                                "state": status,
                                "restarts": int(restarts_s) if restarts_s.isdigit() else 0,
                                "version": version,
                                "observed_at": observed_at,
                            }
                        )
                        pods[pod_name] = existing
            elif words == ["kubectl", "get", "events"] and isinstance(data, list):
                events = []
                for raw_item in cast(list[object], data):
                    item = _as_dict(raw_item)
                    if item is not None:
                        events.append({**item, "observed_at": observed_at})
            elif words[:2] == ["edges", "list"] and isinstance(data, list):
                for raw_item in cast(list[object], data):
                    item = _as_dict(raw_item)
                    if item is not None:
                        caller = str(item.get("caller", ""))
                        callee = str(item.get("callee", ""))
                        if caller and callee:
                            ensure_service(caller)
                            ensure_service(callee)
                            edges[(caller, callee)] = {**item, "observed_at": observed_at}
            elif (
                words[:2] == ["changes", "list"]
                or words[:2] == ["config", "history"]
                or words[:3] == ["kubectl", "rollout", "history"]
            ) and isinstance(data, list):
                for raw_item in cast(list[object], data):
                    item = _as_dict(raw_item)
                    if item is not None and isinstance(item.get("id"), str):
                        cid = str(item["id"])
                        ensure_service(str(item.get("service", "")))
                        # Global headers must not erase details already discovered
                        # through a scoped history query.
                        changes[cid] = {**changes.get(cid, {}), **item, "observed_at": observed_at}
            else:
                msg_data = _as_dict(data)
                if msg_data is not None and "message" in msg_data:
                    revision = str(msg_data.get("revision", ""))
                    if revision:
                        reverted_ids.add(revision)
                    actions_taken.append(
                        {
                            "step": record.step,
                            "command": cmd,
                            "observed_at": observed_at,
                            "minute": current_minute,
                            "message": msg_data.get("message"),
                            "revision": revision,
                        }
                    )

        elif call.name == "query_metrics" and isinstance(data, list):
            metric = str(call.args.get("metric", ""))
            by = str(call.args.get("by", "service"))
            is_series = bool(call.args.get("series", False))
            rows = [
                d for raw_item in cast(list[object], data) if (d := _as_dict(raw_item)) is not None
            ]
            if is_series:
                bucket = series_map.setdefault(metric, [])
                seen_pts = {(pt.get("name"), pt.get("minute")) for pt in bucket}
                for item in rows:
                    key = (item.get("name"), item.get("minute"))
                    if key not in seen_pts:
                        bucket.append(item)
                        seen_pts.add(key)
                    if by == "service" and isinstance(item.get("name"), str):
                        ensure_service(str(item["name"]))
                latest_by_name: dict[str, tuple[int, float]] = {}
                for item in rows:
                    nm = str(item.get("name", ""))
                    mn = item.get("minute")
                    val = item.get("value")
                    if nm and isinstance(mn, int) and isinstance(val, (int, float)):
                        prev = latest_by_name.get(nm)
                        if prev is None or mn >= prev[0]:
                            latest_by_name[nm] = (mn, float(val))
                for nm, (mn, val) in latest_by_name.items():
                    if by == "pod":
                        svc_name = _service_from_pod(nm)
                        ensure_service(svc_name)
                        pod_rec = pods.setdefault(
                            nm, {"pod": nm, "service": svc_name, "metrics": {}}
                        )
                        pod_rec["metrics"][metric] = val
                        pod_rec["observed_at"] = mn
                    else:
                        svc = ensure_service(nm)
                        svc["metrics"][metric] = val
                        svc["observed_at"] = mn
            else:
                for item in rows:
                    nm = str(item.get("name", ""))
                    val = item.get("value")
                    if not nm or not isinstance(val, (int, float)):
                        continue
                    if by == "pod":
                        svc_name = _service_from_pod(nm)
                        ensure_service(svc_name)
                        pod_rec = pods.setdefault(
                            nm, {"pod": nm, "service": svc_name, "metrics": {}}
                        )
                        pod_rec["metrics"][metric] = float(val)
                        pod_rec["observed_at"] = observed_at
                    else:
                        svc = ensure_service(nm)
                        svc["metrics"][metric] = float(val)
                        svc["observed_at"] = observed_at

        elif call.name == "query_edges" and isinstance(data, list):
            for raw_item in cast(list[object], data):
                item = _as_dict(raw_item)
                if item is not None:
                    caller = str(item.get("caller", ""))
                    callee = str(item.get("callee", ""))
                    if caller and callee:
                        ensure_service(caller)
                        ensure_service(callee)
                        edges[(caller, callee)] = {**item, "observed_at": observed_at}

        elif call.name == "list_alerts" and isinstance(data, list):
            alerts = []
            for raw_item in cast(list[object], data):
                item = _as_dict(raw_item)
                if item is not None:
                    svc_name = str(item.get("service", ""))
                    if svc_name:
                        svc = ensure_service(svc_name)
                        err = item.get("error_ratio")
                        if isinstance(err, (int, float)):
                            svc["metrics"].setdefault("error_ratio", float(err))
                    alerts.append({**item, "observed_at": observed_at})

        elif call.name == "search_logs" and isinstance(data, list):
            svc_name = str(call.args.get("service", ""))
            if svc_name:
                ensure_service(svc_name)
            for raw_item in cast(list[object], data):
                item = _as_dict(raw_item)
                if item is not None:
                    entry: dict[str, Any] = {
                        "service": svc_name,
                        "observed_at": observed_at,
                        **item,
                    }
                    if entry not in logs:
                        logs.append(entry)

    sorted_changes = sorted(
        ({**chg, "reverted": cid in reverted_ids} for cid, chg in changes.items()),
        key=lambda r: int(r["minute"]) if isinstance(r.get("minute"), int) else 0,
    )
    return {
        "current_minute": current_minute,
        "topology_known": topology_known,
        "services": list(services.values()),
        "pods": list(pods.values()),
        "edges": list(edges.values()),
        "alerts": alerts,
        "changes": sorted_changes,
        "events": events,
        "logs": logs[-120:],
        "series": series_map,
        "actions_taken": actions_taken,
    }


def _build_traces(
    world: World,
    pod_frame: tuple[PodSample, ...],
    edge_frame: tuple[EdgeSample, ...],
    selected_minute: int,
) -> list[dict[str, Any]]:
    """Construct synthetic causal call-path waterfalls per gateway branch from fluid telemetry."""
    svc_map: dict[str, Service] = {s.name: s for s in world.services}
    pods_by_svc: dict[str, list[PodSample]] = {}
    for p in pod_frame:
        pods_by_svc.setdefault(p.service, []).append(p)
    edge_map: dict[tuple[str, str], EdgeSample] = {(e.caller, e.callee): e for e in edge_frame}

    gateway = svc_map.get("edge-gateway")
    if gateway is None:
        return []

    chains: list[list[tuple[str | None, str]]] = []
    pending: list[list[tuple[str | None, str]]] = [[(None, gateway.name)]]
    while pending:
        chain = pending.pop()
        caller = chain[-1][1]
        visited = {name for _, name in chain}
        dependencies = [
            dep for dep in svc_map[caller].dependencies if dep in svc_map and dep not in visited
        ]
        if not dependencies:
            chains.append(chain)
        else:
            pending.extend([*chain, (caller, dep)] for dep in reversed(dependencies))

    traces: list[dict[str, Any]] = []
    for idx, chain in enumerate(chains):
        top_callee = chain[1][1] if len(chain) > 1 else gateway.name
        branch_label = top_callee.split("-")[0] if "-" in top_callee else top_callee
        spans: list[dict[str, Any]] = []
        cursor_ms = 0.0
        max_err = 0.0
        bottleneck = "healthy"

        for depth, (parent_svc, svc_name) in enumerate(chain):
            svc_obj = svc_map[svc_name]
            svc_pods = pods_by_svc.get(svc_name, [])
            p99_ms = max((p.p99_latency for p in svc_pods), default=svc_obj.base_latency) * 1000.0
            avg_err = sum(p.error_ratio for p in svc_pods) / len(svc_pods) if svc_pods else 0.0
            avg_mem = sum(p.memory for p in svc_pods) / len(svc_pods) if svc_pods else 150.0
            total_queue = sum(p.queue_depth for p in svc_pods)
            worst_pod = max(svc_pods, key=lambda p: p.error_ratio, default=None)
            pod_states = sorted({p.state for p in svc_pods})
            pod_status = (
                "OOMKilled"
                if "OOMKilled" in pod_states
                else "CrashLoopBackOff"
                if "CrashLoopBackOff" in pod_states
                else "Running"
            )
            self_ms = round(svc_obj.base_latency * 1000.0, 2)
            edge = edge_map.get((parent_svc, svc_name)) if parent_svc else None

            timeout_ms = edge.timeout_ms if edge is not None else None
            effective_ms = min(p99_ms, timeout_ms) if timeout_ms is not None else p99_ms
            tags: list[str] = []
            if edge is not None:
                if p99_ms > edge.timeout_ms:
                    tags.append("TIMEOUT")
                if edge.pool_rejection_ratio > 0.01 or edge.concurrency > edge.pool_size:
                    tags.append("POOL_EXHAUSTED")
                if edge.offered_attempt_rate > edge.request_rate * 1.25:
                    tags.append("RETRY_STORM")
            if pod_status == "OOMKilled":
                tags.append("OOMKILLED")
            elif pod_status == "CrashLoopBackOff":
                tags.append("CRASHLOOP")
            if total_queue > 10:
                tags.append("QUEUE_BACKLOG")
            if svc_pods and min(p.error_ratio for p in svc_pods) < 0.01 < max(
                p.error_ratio for p in svc_pods
            ):
                tags.append("PARTIAL_ROLLOUT")

            edge_err = edge.error_ratio if edge is not None else avg_err
            max_err = max(max_err, avg_err, edge_err)
            if tags and bottleneck == "healthy":
                bottleneck = f"{svc_name}: {', '.join(tags)}"
            elif avg_err > 0.02 and bottleneck == "healthy":
                bottleneck = f"{svc_name}: error_ratio={avg_err:.1%}"

            req_rps = (
                round(edge.request_rate, 2)
                if edge is not None
                else round(sum(p.request_rate for p in svc_pods), 2)
            )
            spans.append(
                {
                    "span_id": f"sp-{selected_minute}-{idx}-{depth}",
                    "parent_id": f"sp-{selected_minute}-{idx}-{depth - 1}" if depth > 0 else None,
                    "depth": depth,
                    "caller": parent_svc,
                    "service": svc_name,
                    "kind": svc_obj.kind,
                    "start_ms": round(cursor_ms, 2),
                    "duration_ms": round(p99_ms, 2),
                    "effective_wait_ms": round(effective_ms, 2),
                    "self_ms": self_ms,
                    "timeout_ms": round(timeout_ms, 2) if timeout_ms is not None else None,
                    "error_ratio": round(avg_err, 4),
                    "edge_error_ratio": round(edge.error_ratio, 4) if edge is not None else None,
                    "pod_status": pod_status,
                    "sample_message": worst_pod.message if worst_pod is not None else "",
                    "request_rate": req_rps,
                    "offered_attempt_rate": (
                        round(edge.offered_attempt_rate, 2) if edge is not None else None
                    ),
                    "attempt_rate": round(edge.attempt_rate, 2) if edge is not None else None,
                    "retries": edge.retries if edge is not None else 0.0,
                    "pool_in_use": round(edge.pool_in_use, 2) if edge is not None else None,
                    "pool_size": edge.pool_size if edge is not None else None,
                    "pool_rejection_ratio": (
                        round(edge.pool_rejection_ratio, 4) if edge is not None else 0.0
                    ),
                    "queue_depth": round(total_queue, 1),
                    "memory_mib": round(avg_mem, 1),
                    "tags": tags,
                }
            )
            cursor_ms += max(8.0, self_ms * 0.5)

        total_ms = max(
            (float(s["start_ms"]) + float(s["duration_ms"]) for s in spans),
            default=0.0,
        )
        traces.append(
            {
                "trace_id": f"tr-m{selected_minute}-{branch_label}-{idx}",
                "branch": f"{branch_label} / {chain[-1][1]}",
                "minute": selected_minute,
                "root_service": "edge-gateway",
                "leaf_service": chain[-1][1],
                "total_latency_ms": round(total_ms, 2),
                "max_error_ratio": round(max_err, 4),
                "has_error": max_err > 0.01,
                "bottleneck": bottleneck,
                "spans": spans,
            }
        )

    traces.sort(key=lambda t: (-float(t["max_error_ratio"]), -float(t["total_latency_ms"])))
    return traces


def build_god_state(
    task: Task,
    *,
    inspect_minute: int | None = None,
    reveal_secret: bool = False,
) -> dict[str, Any] | None:
    """Extract full cluster topology, telemetry, call-path waterfalls, and loss timeline."""
    if not isinstance(task, OncallTask):
        return None
    world, state = _oncall_internals(task)
    if world is None or not state.history:
        return None

    min_minute = state.history[0][0].minute
    max_minute = state.history[-1][0].minute
    target_minute = (
        max_minute if inspect_minute is None else max(min_minute, min(max_minute, inspect_minute))
    )

    frame_index = len(state.history) - 1
    for i, frame in enumerate(state.history):
        if frame and frame[0].minute == target_minute:
            frame_index = i
            break

    pod_frame = state.history[frame_index]
    edge_frame = state.edge_history[frame_index]
    selected_minute = pod_frame[0].minute
    params = effective_parameters(world, state, selected_minute)

    pods_by_svc: dict[str, list[PodSample]] = {}
    for p in pod_frame:
        pods_by_svc.setdefault(p.service, []).append(p)

    services_out: list[dict[str, Any]] = []
    for svc in world.services:
        spods = pods_by_svc.get(svc.name, [])
        p_eff = params[svc.name]
        req_rate = sum(p.request_rate for p in spods)
        err_ratio = sum(p.error_ratio for p in spods) / len(spods) if spods else 0.0
        p99 = max((p.p99_latency for p in spods), default=0.0)
        mem = sum(p.memory for p in spods) / len(spods) if spods else 150.0
        pool_use = sum(p.pool_in_use for p in spods)
        q_depth = sum(p.queue_depth for p in spods)
        states = {p.state for p in spods}
        status = (
            "OOMKilled"
            if "OOMKilled" in states
            else "CrashLoopBackOff"
            if "CrashLoopBackOff" in states
            else "Running"
        )
        services_out.append(
            {
                "name": svc.name,
                "kind": svc.kind,
                "dependencies": list(svc.dependencies),
                "replicas": svc.replicas,
                "capacity": round(p_eff["capacity"] * svc.replicas, 1),
                "base_latency_ms": round(p_eff["base_latency"] * 1000.0, 1),
                "version": f"v{int(p_eff['version'])}",
                "status": status,
                "request_rate": round(req_rate, 2),
                "error_ratio": round(err_ratio, 4),
                "p99_latency": round(p99, 4),
                "memory": round(mem, 1),
                "pool_in_use": round(pool_use, 2),
                "queue_depth": round(q_depth, 1),
                "restarted_at": state.restart_at.get(svc.name),
                "pods": [asdict(p) for p in spods],
            }
        )

    edges_out: list[dict[str, Any]] = [asdict(e) for e in edge_frame]
    alerts_out = telemetry.alerts(state.history[: frame_index + 1])
    changes_out: list[dict[str, Any]] = [
        {
            **asdict(c),
            "reverted": c.id in state.reverted_changes,
            **({"is_root_cause": c.id == world.change_id} if reveal_secret else {}),
        }
        for c in world.changes
        if c.minute <= state.minute
    ]

    minutes = [f[0].minute for f in state.history]
    actual_loss = [round(loss(f), 4) for f in state.history]
    baseline_loss = _baseline_series(world, max_minute)

    heatmap: dict[str, list[float]] = {s.name: [] for s in world.services}
    for frame in state.history:
        by_s: dict[str, list[float]] = {}
        for p in frame:
            by_s.setdefault(p.service, []).append(p.error_ratio)
        for s in world.services:
            vals = by_s.get(s.name, [0.0])
            heatmap[s.name].append(round(sum(vals) / len(vals), 4))

    secret: dict[str, Any] | None = None
    if reveal_secret:
        expected_rem = (
            "config_revert"
            if world.fault in ("config_push", "pool_exhaustion", "retry_storm")
            else "rollback"
        )
        secret = {
            "culprit": world.culprit,
            "fault": world.fault,
            "change_id": world.change_id,
            "expected_remediation": expected_rem,
        }
        if world.incidents:
            secret["incidents"] = [asdict(incident) for incident in world.incidents]

    pending_dict: dict[str, Any] | None = None
    if state.pending_action is not None:
        op, tgt, rev = state.pending_action
        pending_dict = {"operation": op, "target": tgt, "revision": rev}

    return {
        "minute": state.minute,
        "selected_minute": selected_minute,
        "min_minute": min_minute,
        "max_minute": max_minute,
        "horizon": HORIZON,
        "calls": state.calls,
        "max_steps": task.spec.max_steps,
        "penalty": round(state.penalty, 4),
        "repaired": all(i.change_id in state.reverted_changes for i in world.incidents)
        if world.incidents
        else state.repaired,
        "stampede_until": state.stampede_until if state.stampede_until >= 0 else None,
        "pending_action": pending_dict,
        "reverted_changes": sorted(state.reverted_changes),
        "report": dict(state.report) if state.report is not None else None,
        "incident_reports": state.incident_reports,
        "services": services_out,
        "edges": edges_out,
        "alerts": alerts_out,
        "changes": changes_out,
        "traces": _build_traces(world, pod_frame, edge_frame, selected_minute),
        "timeline": {
            "minutes": minutes,
            "actual_loss": actual_loss,
            "baseline_loss": baseline_loss[: len(minutes)],
            "service_heatmap": heatmap,
        },
        "secret": secret,
    }


__all__ = ["build_fog_state", "build_god_state"]
