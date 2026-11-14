"""A public-observation planner. Find a short route to each departure in turn."""

from heapq import heappop, heappush
from itertools import count, pairwise
from typing import cast

from crucible.core.registry import register_policy
from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec

type Board = tuple[str, ...]
type Move = tuple[int, int, int]
NAMES = ("D", "L", "R")


def estimate(board: Board, target: str) -> int:
    prefix = 0
    for actual, wanted in zip(board[0], target, strict=False):
        if actual != wanted:
            break
        prefix += 1
    # Prefer a clean departure prefix and exposed, already coupled required wagons.
    rest = target[prefix:]
    groups = sum(not any(a + b in t for t in board) for a, b in pairwise(rest))
    blocked = sum(len(t) - 1 - t.index(rest[0]) for t in board if rest and rest[0] in t)
    return int(len(board[0]) > prefix) + groups + int(bool(rest)) + blocked


def route(board: Board, caps: tuple[int, ...], target: str) -> list[Move]:
    """Best first with path cost and a progress estimate; no grade, seed or oracle."""
    serial = count()
    queue = [(0, 0, next(serial), board)]
    best = {board: 0}
    parent: dict[Board, tuple[Board, Move]] = {}
    while queue:
        _, cost, _, cur = heappop(queue)
        if cost != best[cur]:
            continue
        if cur[0] == target:
            moves: list[Move] = []
            while cur in parent:
                cur, move = parent[cur]
                moves.append(move)
            return moves[::-1]
        for i, source in enumerate(cur):
            for j, dest in enumerate(cur):
                if i == j:
                    continue
                for n in range(1, min(len(source), caps[j] - len(dest)) + 1):
                    nxt = list(cur)
                    nxt[i], nxt[j] = source[:-n], dest + source[-n:]
                    state = tuple(nxt)
                    if best.get(state, 10**9) <= cost + 1:
                        continue
                    best[state] = cost + 1
                    parent[state] = cur, (i, j, n)
                    heappush(
                        queue, (cost + 1 + estimate(state, target), cost + 1, next(serial), state)
                    )
    raise ValueError("No route to next manifest")


@register_policy("reference", "last_siding")
class YardPlanner:
    name = "reference"

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self.pending: list[ToolCall] = []

    async def act(self, obs: Observation) -> ToolCall | None:
        if obs.last_result is None:
            return ToolCall(name="inspect")  # same for local and MCP clients
        if not obs.last_result.ok:
            return None
        if self.pending:
            return self.pending.pop(0)
        view = obs.last_result.content
        if not isinstance(view, dict) or view["done"]:
            return None
        stage = len(cast(list[object], view["dispatch_history"]))
        if stage and self.name == "stop_early":
            return ToolCall(name="stop")
        tracks = cast(dict[str, list[str]], view["tracks"])
        caps = cast(dict[str, int], view["capacities"])
        target = "".join(cast(list[list[str]], view["manifests"])[stage])
        path = route(
            tuple("".join(tracks[k]) for k in NAMES), tuple(caps[k] for k in NAMES), target
        )
        self.pending = [
            ToolCall(name="shunt", args={"source": NAMES[i], "destination": NAMES[j], "count": n})
            for i, j, n in path
        ] + [ToolCall(name="dispatch")]
        return self.pending.pop(0)


@register_policy("stop_early", "last_siding")
class StopEarly(YardPlanner):
    """Do real work, stop as soon as the first train has left."""

    name = "stop_early"
