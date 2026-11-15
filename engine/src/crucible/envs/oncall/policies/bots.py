"""Public graph traversal, symptom diagnosis and deliberately naive baselines."""

from pydantic import JsonValue

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec


def _rows(obs: Observation) -> list[dict[str, JsonValue]]:
    content = obs.last_result.content if obs.last_result else None
    if not isinstance(content, dict):
        return []
    data = content.get("data")
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _run(command: str) -> ToolCall:
    return ToolCall(name="run", args={"command": command})


def _metrics(service: str = "*", by: str = "service") -> ToolCall:
    return ToolCall(
        name="query_metrics", args={"metric": "error_ratio", "service": service, "by": by}
    )


class Base:
    name = "base"

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self.stage = 0
        self.services: list[dict[str, JsonValue]] = []
        self.target = ""
        self.change = ""
        self.change_kind = ""
        self.summary = ""
        self.fault = ""
        self.report: dict[str, JsonValue] = {}


def _number(row: dict[str, JsonValue], key: str, default: float = 0) -> float:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else default


def _parameter(change: dict[str, JsonValue], side: str, key: str) -> float | None:
    values = change.get(side)
    value = values.get(key) if isinstance(values, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


@register_policy("graph_walk", "oncall")
class GraphWalk(Base):
    """Trace unhealthy dependencies and match runtime evidence to chronological changes."""

    name = "graph_walk"
    extra_verification = False

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        await super().reset(task_spec, tools, instruction)
        self.health: dict[str, float] = {}
        self.latencies: dict[str, float] = {}
        self.edges: list[dict[str, JsonValue]] = []
        self.changes: list[dict[str, JsonValue]] = []
        self.messages = ""
        self.attempted: set[str] = set()
        self.pod_errors: list[float] = []
        self.onset: float = 5
        self.onsets: dict[str, float] = {}
        self.retry_owner = False
        self.backtrace_change: dict[str, JsonValue] | None = None
        self.backtrace_leaf = ""

    def _choose_target(self) -> None:
        unhealthy = {
            name
            for name in self.health
            if self.health[name] > 0.01 or self.latencies.get(name, 0) > 2
        }
        leaves: list[str] = []
        for service in self.services:
            dependencies = service.get("dependencies")
            if str(service["name"]) in unhealthy and isinstance(dependencies, list):
                if not any(str(dep) in unhealthy for dep in dependencies):
                    leaves.append(str(service["name"]))
        self.target = leaves[0] if leaves else ""
        self.retry_owner = False
        self.backtrace_change = None
        self.backtrace_leaf = self.target
        # Follow excess traffic back only when an observed retry increase coincides with the
        # callee's onset. Routine retry settings on callers of a broken deployment are not causes.
        for edge in self.edges:
            callee = str(edge.get("callee"))
            if callee not in leaves or _number(edge, "error_ratio") <= 0.01:
                continue
            if _number(edge, "offered_attempt_rate") <= 1.5 * _number(edge, "request_rate"):
                continue
            caller = str(edge["caller"])
            # An overloaded callee can cross its error threshold several minutes after the
            # caller starts timing out. The candidate owner's onset is the causal anchor.
            onset = self.onsets.get(caller)
            if onset is None or onset > self.onsets.get(callee, onset):
                continue
            matching = [
                c
                for c in self.changes
                if c.get("service") == caller
                and c.get("callee") == callee
                and str(c.get("id")) not in self.attempted
                and onset - 1 <= _number(c, "minute") <= onset
            ]
            for change in matching:
                retry_before = _parameter(change, "before", "retries")
                retry_after = _parameter(change, "after", "retries")
                timeout_before = _parameter(change, "before", "timeout_ms")
                timeout_after = _parameter(change, "after", "timeout_ms")
                retry_increase = (
                    retry_before is not None
                    and retry_after is not None
                    and retry_after > retry_before
                )
                timeout_crossed = (
                    timeout_before is not None
                    and timeout_after is not None
                    and timeout_after < timeout_before
                    and timeout_after < _number(edge, "latency") * 1000
                )
                if retry_increase or timeout_crossed:
                    self.target = caller
                    self.retry_owner = retry_increase
                    self.backtrace_change = change
                    return

    def _diagnose(self) -> str:
        if "memory limit" in self.messages or "OOM" in self.messages:
            return "memory_leak"
        if "consumer" in self.messages and (
            "exited" in self.messages or "stopped" in self.messages
        ):
            return "queue_backlog"
        if "handler exception" in self.messages or (
            self.pod_errors and min(self.pod_errors) < 0.01 < max(self.pod_errors)
        ):
            return "partial_rollout"
        owned = [e for e in self.edges if e.get("caller") == self.target]
        retry_changes = [
            change
            for change in self.changes
            if change.get("service") == self.target
            and self.onsets.get(self.target, 5) - 1
            <= _number(change, "minute")
            <= self.onsets.get(self.target, 5)
            and _parameter(change, "after", "retries") is not None
            and (_parameter(change, "after", "retries") or 0)
            > (_parameter(change, "before", "retries") or 0)
        ]
        if self.retry_owner or (
            retry_changes
            and any(
                _number(e, "offered_attempt_rate") > 1.5 * _number(e, "request_rate")
                and _number(e, "error_ratio") > 0.01
                for e in owned
            )
        ):
            return "retry_storm"
        if "pool exhausted" in self.messages or any(
            _number(e, "concurrency") > _number(e, "pool_size") for e in owned
        ):
            return "pool_exhaustion"
        if "timeout" in self.messages or any(
            _number(e, "latency") * 1000 > _number(e, "timeout_ms") for e in owned
        ):
            return "config_push"
        return ""

    def _repair(self) -> ToolCall:
        self.fault = self._diagnose()
        config = self.fault in ("config_push", "pool_exhaustion", "retry_storm")
        candidates = [
            c
            for c in self.changes
            if c.get("service") == self.target
            and c.get("kind") == ("config" if config else "deploy")
            and str(c.get("id")) not in self.attempted
        ]
        if config:
            keys = {
                "config_push": ("timeout_ms",),
                "pool_exhaustion": ("pool_size", "rate_multiplier"),
                "retry_storm": ("retries",),
            }[self.fault]
            relevant: list[dict[str, JsonValue]] = []
            for change in candidates:
                for key in keys:
                    before, after = (
                        _parameter(change, "before", key),
                        _parameter(change, "after", key),
                    )
                    if (
                        before is not None
                        and after is not None
                        and (
                            after < before if key in ("pool_size", "timeout_ms") else after > before
                        )
                    ):
                        relevant.append(change)
                        break
            candidates = relevant
        # Prefer a change at or before the observed onset; never blindly undo the newest decoy.
        prior = [c for c in candidates if _number(c, "minute") <= self.onset]
        candidates = prior or candidates
        if not candidates:
            return ToolCall(name="resolve", args={"report": {}})
        change = max(candidates, key=lambda c: _number(c, "minute"))
        self.change = str(change["id"])
        self.attempted.add(self.change)
        self.report = {
            "service": self.target,
            "fault_kind": self.fault,
            "change_id": self.change,
            "remediation": "config_revert" if config else "rollback",
        }
        return _run(
            f"config revert {self.target} {self.change}"
            if config
            else f"kubectl rollout undo deploy/{self.target} --to-revision={self.change}"
        )

    async def act(self, obs: Observation) -> ToolCall | None:
        stage = self.stage
        self.stage += 1
        rows = _rows(obs)
        if stage == 0:
            return _run("kubectl get services")
        if stage == 1:
            self.services = rows
            return _metrics()
        if stage == 2:
            self.health = {str(r["name"]): _number(r, "value") for r in rows}
            return ToolCall(name="query_metrics", args={"metric": "p99_latency", "service": "*"})
        if stage == 3:
            self.latencies = {str(r["name"]): _number(r, "value") for r in rows}
            return ToolCall(name="query_edges", args={"service": "*"})
        if stage == 4:
            self.edges = rows
            return ToolCall(
                name="query_metrics",
                args={
                    "metric": "error_ratio",
                    "service": "*",
                    "window": 50,
                    "series": True,
                },
            )
        if stage == 5:
            # Retain evidence already observed when the rolling query window moves forward.
            for row in rows:
                if _number(row, "value") > 0.01:
                    name, minute = str(row["name"]), _number(row, "minute")
                    self.onsets[name] = min(self.onsets.get(name, minute), minute)
            return _run("changes list")
        if stage == 6:
            self.changes = rows
            self._choose_target()
            if not self.target:
                return ToolCall(name="resolve", args={"report": self.report})
            self.onset = self.onsets.get(self.target, 5)
            return _metrics(self.target, "pod")
        if stage == 7:
            self.pod_errors = [_number(row, "value") for row in rows]
            return ToolCall(name="search_logs", args={"service": self.target, "level": "ERROR"})
        if stage == 8:
            self.messages = " ".join(str(row.get("message", "")) for row in rows)
            if self.backtrace_change is not None:
                self.stage = 15
                return ToolCall(
                    name="query_edges",
                    args={
                        "service": self.target,
                        "window": 1,
                        "end_minute": int(self.onset) - 1,
                    },
                )
            return self._repair()
        if stage == 9:
            return ToolCall(name="wait", args={"minutes": 30})
        if stage == 10:
            return _metrics()
        if stage == 11:
            self.health = {str(row["name"]): _number(row, "value") for row in rows}
            return ToolCall(name="query_metrics", args={"metric": "p99_latency", "service": "*"})
        if stage == 12:
            if any(value > 0.01 for value in self.health.values()) or any(
                _number(row, "value") > 2 for row in rows
            ):
                self.stage = 14
                return ToolCall(
                    name="query_metrics",
                    args={
                        "metric": "queue_depth",
                        "service": "*",
                        "window": 15,
                        "series": True,
                    },
                )
            if self.extra_verification and self.fault == "memory_leak":
                return ToolCall(
                    name="query_metrics",
                    args={
                        "metric": "memory",
                        "service": self.target,
                        "window": 15,
                        "series": True,
                    },
                )
            return ToolCall(name="resolve", args={"report": self.report})
        if stage == 13 and self.extra_verification:
            values = [_number(row, "value") for row in rows]
            if values and values[-1] > values[0] + 1:
                self.stage = 1
                return _run("kubectl get services")
            return ToolCall(name="resolve", args={"report": self.report})
        if stage == 14:
            series: dict[str, list[float]] = {}
            for row in rows:
                series.setdefault(str(row["name"]), []).append(_number(row, "value"))
            pending = [values for values in series.values() if values and values[-1] > 0]
            if pending and all(values[-1] < values[0] - 1 for values in pending):
                # A repaired consumer must process its remaining work; another rollback cannot
                # erase it. Confirm the observed backlog continues to fall before waiting again.
                self.stage = 10
                return ToolCall(name="wait", args={"minutes": 30})
            self.stage = 1
            self.target = ""
            return _run("kubectl get services")
        if stage == 15:
            change = self.backtrace_change
            self.backtrace_change = None
            historical = next(
                (
                    edge
                    for edge in rows
                    if change is not None and edge.get("callee") == change.get("callee")
                ),
                None,
            )
            before = _parameter(change, "before", "timeout_ms") if change else None
            after = _parameter(change, "after", "timeout_ms") if change else None
            threshold_crossed = (
                historical is not None
                and before is not None
                and after is not None
                and _number(historical, "error_ratio") <= 0.01
                and after < _number(historical, "latency") * 1000 <= before
            )
            if threshold_crossed:
                self.stage = 9
                return self._repair()
            # The prior callee was already unhealthy, or the change did not cross the observed
            # threshold. Inspect that callee instead of blaming its caller's retries.
            self.target = self.backtrace_leaf
            self.onset = self.onsets.get(self.target, self.onset)
            self.retry_owner = False
            self.stage = 7
            return _metrics(self.target, "pod")
        return None


@register_policy("reference", "oncall")
class Reference(GraphWalk):
    """Graph walk with a post-recovery memory-slope check for leak incidents."""

    name = "reference"
    extra_verification = True


@register_policy("newest_change", "oncall")
class NewestChange(GraphWalk):
    """Diagnostic baseline: graph traversal, then newest local change without causal matching."""

    name = "newest_change"

    def _repair(self) -> ToolCall:
        candidates = [
            change
            for change in self.changes
            if change.get("service") == self.target and str(change.get("id")) not in self.attempted
        ]
        if not candidates:
            return ToolCall(name="resolve", args={"report": self.report})
        change = max(candidates, key=lambda row: _number(row, "minute"))
        self.change = str(change["id"])
        self.attempted.add(self.change)
        self.fault = self._diagnose()
        config = change.get("kind") == "config"
        self.report = {
            "service": self.target,
            "fault_kind": self.fault,
            "change_id": self.change,
            "remediation": "config_revert" if config else "rollback",
        }
        return _run(
            f"config revert {self.target} {self.change}"
            if config
            else f"kubectl rollout undo deploy/{self.target} --to-revision={self.change}"
        )


@register_policy("loudest_alert", "oncall")
class LoudestAlert(Base):
    name = "loudest_alert"

    async def act(self, obs: Observation) -> ToolCall | None:
        stage = self.stage
        self.stage += 1
        if stage == 0:
            return ToolCall(name="list_alerts", args={})
        if stage == 1:
            rows = _rows(obs)
            if not rows:
                return None
            self.target = str(max(rows, key=lambda row: int(str(row["severity"])))["service"])
            return ToolCall(name="search_logs", args={"service": self.target})
        if stage == 2:
            return _run(f"kubectl rollout restart deploy/{self.target}")
        if stage == 3:
            return _run(f"kubectl rollout undo deploy/{self.target}")
        if stage == 4:
            return ToolCall(name="resolve", args={"report": {"service": self.target}})
        return None


class Sweep(Base):
    operations = ("restart", "undo")

    async def act(self, obs: Observation) -> ToolCall | None:
        stage = self.stage
        self.stage += 1
        if stage == 0:
            return _run("kubectl get services")
        if stage == 1:
            self.services = _rows(obs)
        index = stage - 1
        if index < len(self.services) * len(self.operations):
            service = self.services[index // len(self.operations)]
            operation = self.operations[index % len(self.operations)]
            return _run(f"kubectl rollout {operation} deploy/{service['name']}")
        if index == len(self.services) * len(self.operations):
            return ToolCall(name="resolve", args={"report": {}})
        return None


@register_policy("shotgun", "oncall")
class Shotgun(Sweep):
    name = "shotgun"


@register_policy("restart_all", "oncall")
class RestartAll(Sweep):
    name = "restart_all"
    operations = ("restart",)


@register_policy("rollback_all", "oncall")
class RollbackAll(Sweep):
    name = "rollback_all"
    operations = ("undo",)


@register_policy("guess_report", "oncall")
class GuessReport(Base):
    name = "guess_report"

    async def act(self, obs: Observation) -> ToolCall | None:
        return ToolCall(
            name="resolve",
            args={"report": {"service": "edge-gateway", "fault_kind": "partial_rollout"}},
        )
