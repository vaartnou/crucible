"""The public rules. Strings are compact immutable tracks, buffer to accessible end."""

from collections.abc import Iterator
from dataclasses import dataclass

type Tracks = tuple[str, str, str]
type Caps = tuple[int, int, int]
type Move = tuple[int, int, int]
TRACKS = ("D", "L", "R")


def shunts(board: Tracks, caps: Caps) -> Iterator[Move]:
    for i, source in enumerate(board):
        for j, dest in enumerate(board):
            if i != j:
                for n in range(1, min(len(source), caps[j] - len(dest)) + 1):
                    yield i, j, n


def transfer(board: Tracks, move: Move) -> Tracks:
    """Apply an already validated shunt, preserving left-to-right order."""
    i, j, n = move
    tracks = list(board)
    tracks[i], tracks[j] = board[i][:-n], board[j] + board[i][-n:]
    return tracks[0], tracks[1], tracks[2]


@dataclass(frozen=True)
class Layout:
    board: Tracks
    caps: Caps
    manifests: tuple[str, str]
    optimal: int
