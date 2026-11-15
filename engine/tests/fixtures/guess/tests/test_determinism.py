import asyncio
import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from crucible.core.episode import EpisodeResult
from crucible.core.tools import ToolCall
from crucible.core.transcript import TIME_FIELDS
from crucible.testing import ScriptedPolicy, play


def guess_call(n: int) -> ToolCall:
    return ToolCall(name="guess", args={"n": n})


def _normalised(path: Path) -> list[dict[str, object]]:
    """Transcript records with the time fields removed."""
    records: list[dict[str, object]] = [json.loads(ln) for ln in path.read_text().splitlines()]
    for record in records:
        for field in TIME_FIELDS:
            record.pop(field, None)
    return records


def _run(seed: int, guesses: list[int]) -> tuple[EpisodeResult, list[dict[str, object]]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        result = asyncio.run(play(ScriptedPolicy([guess_call(n) for n in guesses]), seed, path))
        return result, _normalised(path)


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**31),
    guesses=st.lists(st.integers(min_value=-5, max_value=105), max_size=15),
)
def test_same_input_same_transcript_and_score(seed: int, guesses: list[int]) -> None:
    first, records_1 = _run(seed, guesses)
    second, records_2 = _run(seed, guesses)
    assert records_1 == records_2
    assert first.score == second.score
    assert first.steps == second.steps
    assert first.stop_reason == second.stop_reason
