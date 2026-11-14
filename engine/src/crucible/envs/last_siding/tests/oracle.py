"""Offline calibration. Reverse 0-1 BFS enumerates every completable state."""

from collections import deque
from functools import lru_cache

from crucible.envs.last_siding.yard import Caps, Move, Tracks, shunts, transfer

EMPTY: Tracks = ("", "", "")


@lru_cache(maxsize=6)
def distances(caps: Caps, manifests: tuple[str, str]) -> dict[Tracks, int]:
    dist = {EMPTY: 0}
    todo = deque([EMPTY])
    while todo:
        board = todo.popleft()
        cost = dist[board]
        # The number of remaining wagons uniquely identifies the dispatch phase.
        remaining = sum(map(len, board))
        if not board[0] and remaining in (0, len(manifests[1])):
            train = manifests[1 if remaining == 0 else 0]
            pred = (train, *board[1:])
            if len(train) <= caps[0] and dist.get(pred, 10**9) > cost:
                dist[pred] = cost
                todo.appendleft(pred)
        for move in shunts(board, caps):
            pred = transfer(board, move)  # every shunt has a same-cost inverse
            if dist.get(pred, 10**9) > cost + 1:
                dist[pred] = cost + 1
                todo.append(pred)
    return dist


def continuation(board: Tracks, caps: Caps, manifests: tuple[str, str]) -> list[Move | None]:
    dist = distances(caps, manifests)
    path: list[Move | None] = []
    while board != EMPTY:
        stage = int(sum(map(len, board)) == len(manifests[1]))
        dispatched = ("", board[1], board[2])
        if board[0] == manifests[stage] and dist.get(dispatched) == dist[board]:
            path.append(None)
            board = dispatched
            continue
        move = next(
            m for m in shunts(board, caps) if dist.get(transfer(board, m)) == dist[board] - 1
        )
        path.append(move)
        board = transfer(board, move)
    return path
