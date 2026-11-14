"""Last Siding: public single-ended yard, two ordered departures."""

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from crucible.core.registry import register_task
from crucible.core.scoring import Score, load_scoring_config
from crucible.core.task import Task, TaskChecks, TaskSpec
from crucible.core.tools import (
    Observation,
    ToolCall,
    ToolKind,
    ToolResult,
    ToolSpec,
    tool_map,
    validate_call,
)
from crucible.envs.last_siding.world import draw
from crucible.envs.last_siding.yard import TRACKS, Layout, Move, Tracks, shunts, transfer

BUDGET = 16
type Track = Literal["D", "L", "R"]


class Weights(BaseModel):
    partial: float
    completion: float
    efficiency: float


WEIGHTS, SCORE_HASH = load_scoring_config(Path(__file__).with_name("scoring.yaml"), Weights)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Shunt(NoArgs):
    source: Track
    destination: Track
    count: int = Field(ge=1)
    top: str | None = Field(default=None, description="Optional check: ID at the accessible end.")


class Preview(NoArgs):
    move: Shunt | Literal["dispatch"]


DEFINITIONS: tuple[tuple[str, str, ToolKind, type[BaseModel]], ...] = (
    ("inspect", "Read board, rules of access, history and legal moves.", "read", NoArgs),
    ("shunt", "Move the rightmost count wagons, preserving order.", "action", Shunt),
    (
        "dispatch",
        "Send the next train only if the whole departure track matches.",
        "action",
        NoArgs,
    ),
    ("preview", "Read the state after one hypothetical shunt or dispatch.", "read", Preview),
    ("stop", "End this episode now.", "end", NoArgs),
)
TOOLS = [ToolSpec(name=n, description=d, kind=k, args_model=m) for n, d, k, m in DEFINITIONS]
SPECS = tool_map(TOOLS)


@register_task
class LastSidingTask(Task):
    spec: ClassVar[TaskSpec] = TaskSpec(
        id="last_siding",
        version=1,
        max_steps=64,
        title="Last Siding",
        description="Assemble and dispatch two ordered manifests in a capacity-limited rail yard.",
        keywords=("planning", "rail-yard", "reversible-actions"),
        timeout_sec=600,
    )
    checks: ClassVar[TaskChecks] = TaskChecks(exploit_policies=("stop_early",))
    scoring_hash: ClassVar[str] = SCORE_HASH

    def __init__(self) -> None:
        self.layout: Layout | None = None
        self.board: Tracks = ("", "", "")
        self.sent = self.used = 0
        self.stopped = False

    async def reset(
        self, seed: int, difficulty: int = 0, *, salt: str | None = None
    ) -> Observation:
        self.layout = draw(seed, salt)
        self.board = self.layout.board
        self.sent = self.used = 0
        self.stopped = False
        return Observation(text=self.instruction(), data=self._view(self.board, 0, 0))

    def tools(self) -> list[ToolSpec]:
        return list(TOOLS)

    def instruction(self) -> str:
        return (
            "Dispatch both manifests in order. Call inspect to see the yard. Each list runs "
            "from buffer (left) to accessible end (right). Shunt moves a suffix to the right "
            "end of another track without reversing it. Example: D=[A,B,C], L=[X]; shunt "
            "D to L count 2 gives D=[A], L=[X,B,C]. Locomotive travel is included; no "
            "orientation. Capacity is a hard limit. Dispatch removes the ENTIRE D track only "
            "when it equals the next manifest. 16 successful shunts; dispatch costs no shunts, "
            "even at zero remaining. The harness allows 64 total calls including reads, invalid "
            "calls, previews and dispatches. Preview is hypothetical; inspect reads the real "
            "board. Stop ends play. Score: none sent 0; first only 0.10; both sent "
            "0.80 + 0.20 * minimum shunts / shunts used."
        )

    def _over(self, board: Tracks, used: int, sent: int) -> bool:
        assert self.layout
        return (
            self.stopped
            or sent == 2
            or (used == BUDGET and board[0] != self.layout.manifests[sent])
        )

    def done(self) -> bool:
        return self.layout is not None and self._over(self.board, self.used, self.sent)

    def _view(self, board: Tracks, used: int, sent: int) -> dict[str, JsonValue]:
        assert self.layout
        caps, manifests = self.layout.caps, self.layout.manifests
        over = self._over(board, used, sent)
        diagram = "\n".join(
            f"{name} | {' '.join(track) or '-'} >  {len(track)}/{cap}"
            for name, track, cap in zip(TRACKS, board, caps, strict=True)
        )
        legal: list[JsonValue] = []
        if not over:
            if used < BUDGET:
                legal.extend(
                    {
                        "name": "shunt",
                        "args": {"source": TRACKS[i], "destination": TRACKS[j], "count": n},
                    }
                    for i, j, n in shunts(board, caps)
                )
            if board[0] == manifests[sent]:
                legal.append({"name": "dispatch", "args": {}})
            legal.append({"name": "stop", "args": {}})
        return {
            "diagram": diagram,
            "accessible_end": "right",
            "tracks": {k: list(v) for k, v in zip(TRACKS, board, strict=True)},
            "capacities": dict(zip(TRACKS, caps, strict=True)),
            "manifests": [list(m) for m in manifests],
            "dispatch_history": [list(m) for m in manifests[:sent]],
            "shunts_used": used,
            "shunts_remaining": BUDGET - used,
            "shunt_budget": BUDGET,
            "tool_call_limit": self.spec.max_steps,
            "legal_actions": legal,
            "done": over,
        }

    async def step(self, call: ToolCall) -> ToolResult:
        if not self.layout or self.done():
            return ToolResult(ok=False, error="Episode is not active")
        args = validate_call(SPECS, call)
        if isinstance(args, ToolResult):
            return args
        preview = isinstance(args, Preview)
        action = args.move if isinstance(args, Preview) else args
        board, used, sent = self.board, self.used, self.sent
        if isinstance(action, Shunt):
            i, j = TRACKS.index(action.source), TRACKS.index(action.destination)
            move: Move = (i, j, action.count)
            if used >= BUDGET or move not in shunts(board, self.layout.caps):
                return ToolResult(ok=False, error="Illegal transfer; inspect the legal actions")
            if action.top is not None and action.top != board[i][-1]:
                return ToolResult(ok=False, error="Wagon ID does not match accessible end")
            board, used = transfer(board, move), used + 1
        elif call.name == "dispatch" or action == "dispatch":
            if board[0] != self.layout.manifests[sent]:
                return ToolResult(ok=False, error="D does not exactly match the next manifest")
            board, sent = ("", board[1], board[2]), sent + 1
        elif call.name == "stop":
            self.stopped = True
        if not preview:
            self.board, self.used, self.sent = board, used, sent
        view = self._view(board, used, sent)
        return ToolResult(ok=True, content={"hypothetical": view} if preview else view)

    async def grade(self) -> Score:
        assert self.layout
        total = 0.0 if self.sent == 0 else WEIGHTS.partial
        if self.sent == 2:
            total = WEIGHTS.completion + WEIGHTS.efficiency * self.layout.optimal / self.used
        return Score(
            total=total,
            breakdown={
                "dispatches": self.sent,
                "actual_shunts": self.used,
                "optimal_shunts": self.layout.optimal,
            },
            flags={"solved": self.sent == 2},
        )

    def solved(self, score: Score) -> bool:
        return score.flags.get("solved", False)
