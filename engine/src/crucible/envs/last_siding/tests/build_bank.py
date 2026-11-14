"""Build the canonical layout bank. Run from a source checkout (never needed at reset)."""

import json
from pathlib import Path

from crucible.envs.last_siding.tests.oracle import distances
from crucible.envs.last_siding.yard import Caps, Tracks, transfer

BANK = Path(__file__).resolve().parents[1] / "layouts.json"
DEMO: Tracks = ("ED", "BC", "A")
DEMO_CAPS: Caps = (3, 3, 3)
DEMO_MANIFESTS = ("AB", "CDE")


def canonical(b: Tracks, caps: Caps) -> tuple[object, ...]:
    # IDs already encode position in the concatenated manifests. Sidings may swap.
    return (caps[0], b[0], *sorted(zip(caps[1:], b[1:], strict=True)))


def build() -> list[list[object]]:
    rows: list[list[object]] = []
    seen: set[tuple[tuple[str, str], tuple[object, ...]]] = {
        (DEMO_MANIFESTS, canonical(DEMO, DEMO_CAPS))
    }
    for wagons in ("ABCDE", "ABCDEF"):
        for split in (2, 3, 4):
            man = (wagons[:split], wagons[split:])
            if min(map(len, man)) < 2:
                continue
            for departure in range(max(map(len, man)), max(map(len, man)) + 2):
                for left in (2, 3, 4):
                    for right in (2, 3, 4):
                        caps = (departure, left, right)
                        d = distances(caps, man)
                        ctrl = distances((departure, left + 1, right), man)
                        for board, cost in sorted(d.items()):
                            key = (man, canonical(board, caps))
                            if key in seen or cost < 5 or board[0] == man[0]:
                                continue
                            if sum(map(len, board)) != len(wagons) or len(board[1]) != left - 1:
                                continue
                            # A one-wagon shunt stages an ordered pair on the last L slot.
                            # A needed first-train wagon underneath must later be uncovered.
                            if not any(w in man[0] for w in board[1][:-1]):
                                continue
                            for src in (0, 2):
                                if not board[src]:
                                    continue
                                pair = board[1][-1] + board[src][-1]
                                if pair not in man[1]:
                                    continue
                                after = transfer(board, (src, 1, 1))
                                if d[after] + 1 > cost and ctrl[after] + 1 == ctrl[board]:
                                    seen.add(key)
                                    rows.append([board, caps, man, cost])
                                    break
    return rows


if __name__ == "__main__":
    rows = build()
    BANK.write_text("[\n" + ",\n".join(json.dumps(r) for r in rows) + "\n]\n", encoding="utf8")
    print(f"{len(rows)} distinct certified layouts")
