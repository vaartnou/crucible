"""Budgeted public tools for oncall@1."""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from crucible.core.registry import register_task
from crucible.core.scoring import Score
from crucible.core.task import Task, TaskChecks, TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolResult, ToolSpec, tool_map, validate_call
from crucible.envs.oncall import commands, telemetry
from crucible.envs.oncall.faults import apply_action
from crucible.envs.oncall.grader import SCORING_HASH, grade
from crucible.envs.oncall.model import State, advance, solve
from crucible.envs.oncall.world import FAULTS, HORIZON, World, make_world


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunArgs(Args):
    command: str = Field(min_length=1, max_length=500)


class MetricsArgs(Args):
    metric: telemetry.Metric
    service: str
    by: Literal["service", "pod"] = "service"
    window: int = Field(default=1, ge=1, le=150)
    end_minute: int | None = Field(default=None, ge=-30, le=150)
    series: bool = False


class EdgesArgs(Args):
    service: str = "*"
    window: int = Field(default=1, ge=1, le=150)
    end_minute: int | None = Field(default=None, ge=-30, le=150)
    series: bool = False


class LogsArgs(Args):
    service: str
    query: str = ""
    level: Literal["", "INFO", "ERROR"] = ""
    since: int = Field(default=10, ge=1, le=150)
    page: int = Field(default=0, ge=0, le=100)


class WaitArgs(Args):
    minutes: int = Field(ge=1, le=30)


class Report(Args):
    service: str = ""
    fault_kind: Literal[
        "",
        "partial_rollout",
        "config_push",
        "memory_leak",
        "pool_exhaustion",
        "retry_storm",
        "queue_backlog",
    ] = ""
    change_id: str = ""
    remediation: str = ""


class ResolveArgs(Args):
    report: Report = Field(default_factory=Report)


TOOLS = [
    ToolSpec(
        name="run",
        description="Run a supported command. Reads cost 1 minute, actions 3.",
        kind="action",
        args_model=RunArgs,
    ),
    ToolSpec(
        name="query_metrics",
        description="Query recorded metrics. service='*' selects all services. Costs 1 minute.",
        kind="read",
        args_model=MetricsArgs,
    ),
    ToolSpec(
        name="query_edges",
        description="Recorded caller/callee traffic, latency and limits; costs one minute.",
        kind="read",
        args_model=EdgesArgs,
    ),
    ToolSpec(
        name="search_logs",
        description="Search recorded logs, newest first, 20 lines per page. Costs 1 minute.",
        kind="read",
        args_model=LogsArgs,
    ),
    ToolSpec(
        name="list_alerts",
        description="Current alerts from the last five minutes. Costs 1 minute.",
        kind="read",
        args_model=Args,
    ),
    ToolSpec(
        name="wait", description="Advance time by 1-30 minutes.", kind="clock", args_model=WaitArgs
    ),
    ToolSpec(
        name="resolve",
        description="Close with a diagnosis. Recovery is tested for thirty additional minutes.",
        kind="end",
        args_model=ResolveArgs,
    ),
]


@register_task
class OncallTask(Task):
    spec: ClassVar[TaskSpec] = TaskSpec(
        id="oncall",
        version=1,
        max_steps=80,
        title="On-call gym",
        description="Diagnose cascading failures in a deterministic simulated production system.",
        keywords=("on-call", "sre"),
    )
    scoring_hash: ClassVar[str] = SCORING_HASH
    checks: ClassVar[TaskChecks] = TaskChecks(
        exploit_policies=("shotgun", "restart_all", "rollback_all", "guess_report"),
        oracle_perfect=False,
    )

    def __init__(self) -> None:
        self._world: World | None = None
        self._state = State()

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        self._world = make_world(seed, salt, difficulty)
        self._state = State()
        for minute in range(-30, 6):
            pods, edges = solve(self._world, self._state, minute)
            self._state.history.append(pods)
            self._state.edge_history.append(edges)
        return Observation(text=self.instruction(), data={"minute": 5})

    def tools(self) -> list[ToolSpec]:
        return list(TOOLS)

    def done(self) -> bool:
        return (
            self._state.report is not None
            or self._state.calls >= 80
            or self._state.minute >= HORIZON
        )

    async def step(self, call: ToolCall) -> ToolResult:
        world, state = self._world, self._state
        if world is None or self.done():
            return ToolResult(ok=False, error="Episode unavailable or finished")
        state.calls += 1
        args = validate_call(tool_map(TOOLS), call)
        if isinstance(args, ToolResult):
            return args
        observed_at = state.minute
        content: JsonValue
        duration = 1
        try:
            if isinstance(args, RunArgs):
                content, duration = commands.run(world, state, args.command)
            elif isinstance(args, MetricsArgs):
                self._check_service(args.service, wildcard=True)
                if args.end_minute is not None and args.end_minute > state.minute:
                    raise ValueError("end_minute cannot be in the future")
                content = list(
                    telemetry.metrics(
                        state.history,
                        args.metric,
                        args.service,
                        args.by,
                        args.window,
                        args.end_minute,
                        args.series,
                    )
                )
            elif isinstance(args, EdgesArgs):
                self._check_service(args.service, wildcard=True)
                if args.end_minute is not None and args.end_minute > state.minute:
                    raise ValueError("end_minute cannot be in the future")
                content = list(
                    telemetry.edges(
                        state.edge_history, args.service, args.window, args.end_minute, args.series
                    )
                )
            elif isinstance(args, LogsArgs):
                self._check_service(args.service)
                content = list(
                    telemetry.logs(
                        state.history, args.service, args.query, args.level, args.since, args.page
                    )
                )
            elif isinstance(args, WaitArgs):
                duration = args.minutes
                content = {"waited": args.minutes}
            elif isinstance(args, ResolveArgs):
                state.report = args.report.model_dump()
                duration = 0
                content = {"message": "incident closed"}
            else:
                content = list(telemetry.alerts(state.history))
        except ValueError as exc:
            return ToolResult(ok=False, error=f"error: {exc}")
        elapsed = min(duration, HORIZON - state.minute)
        if state.pending_action is not None:
            operation, target, revision = state.pending_action
            state.pending_action = None
            advance(world, state, max(0, elapsed - 1))
            if elapsed == duration:
                apply_action(world, state, operation, target, revision)
            advance(world, state, min(1, elapsed))
        else:
            advance(world, state, elapsed)
        return ToolResult(
            ok=True, content={"minute": state.minute, "observed_at": observed_at, "data": content}
        )

    def _check_service(self, service: str, wildcard: bool = False) -> None:
        assert self._world is not None
        if not (wildcard and service == "*") and service not in {
            s.name for s in self._world.services
        }:
            raise ValueError("unknown service")

    async def grade(self) -> Score:
        if self._world is None:
            raise ValueError("reset required")
        return grade(self._world, self._state)

    def hidden_strings(self) -> frozenset[str]:
        return frozenset({"culprit", "fingerprint", *FAULTS})

    def solved(self, score: Score) -> bool:
        return score.flags.get("verified", False) and score.flags.get("diagnosis_correct", False)

    def instruction(self) -> str:
        return """You are on call for a simulated production system. Restore service and identify
the triggering change. Time starts at minute 5; you have 80 calls and until minute 150. All reads
cost one simulated minute, changes cost three. Invalid calls count against the call budget. Every
observation comes from the simulated clock, independent of wall time. `observed_at` is the
sample time before a call; `minute` is the clock after its cost.
Commands: kubectl get services; kubectl get pods [-n shop]; kubectl get events;
changes list [SERVICE]; edges list [SERVICE]; kubectl rollout history deploy/SERVICE;
kubectl rollout undo deploy/SERVICE [--to-revision=CHANGE_ID]; kubectl rollout
restart deploy/SERVICE; config history SERVICE; config revert SERVICE CHANGE_ID. Config revert
reverses the named change. Targeted rollout undo reverses the named deployment change;
without a revision, it reverses the newest deployment change. Other shell commands are unsupported.
changes list shows cluster-wide change headers; changes list SERVICE, config history SERVICE,
and kubectl rollout history deploy/SERVICE include before/after values.
Metrics: error_ratio, p99_latency (seconds), request_rate, memory (MiB), pool_in_use, queue_depth.
Query one service or '*' and group by service or pod. Service rates, pool use and queue depth
sum pods; error ratio and memory average pods; latency is the slowest pod. Windows average each
minute's aggregate. Set series=true for per-minute samples;
window selects up to 150 minutes ending at end_minute (default now). History begins at -30.
query_edges uses the same window/end_minute/series controls and reports each caller/callee's
request and attempt rates, observed latency (seconds), concurrency, pool use, timeout, pool limit,
and retries. request_rate excludes retries; offered_attempt_rate includes retry attempts;
attempt_rate counts attempts admitted by the pool. pool_rejection_ratio is the rejected fraction.
Solver residual and iterations report numerical convergence (window maxima).
Logs have 20 lines per page.
Close with resolve(report={service, fault_kind, change_id, remediation}). Fault kinds:
partial_rollout, config_push, memory_leak, pool_exhaustion, retry_storm, queue_backlog.
Remediation: rollback, config_revert, restart. Reports do not perform repairs. Recovery is tested
for thirty more minutes. Scoring rewards avoided failed requests, sustained recovery and correct
diagnosis; wrong actions and cold-cache restarts cost points."""
