"""Float tolerance permits machine roundoff without masking changed evidence."""

import pytest

from crucible.core.tools import ToolResult
from crucible.feedback.observations import observations_match


def test_float_roundoff_requires_explicit_tolerance() -> None:
    left = ToolResult(ok=True, content={"rate": 73.3867932253453, "minute": 44})
    right = ToolResult(ok=True, content={"rate": 73.38679322534531, "minute": 44})
    assert not observations_match(left, right)
    assert observations_match(left, right, 1e-12)
    assert not observations_match(
        left, ToolResult(ok=True, content={"rate": 73.3868, "minute": 44}), 1e-12
    )
    assert not observations_match(
        left, ToolResult(ok=True, content={"rate": 73.3867932253453, "minute": 45}), 1e-12
    )
    with pytest.raises(ValueError):
        observations_match(left, right, 0.01)


def test_structural_strings_and_status_changes_are_always_exact() -> None:
    left = ToolResult(ok=True, content=[{"name": "db", "error": 0.1}])
    for right in (
        ToolResult(ok=True, content=[{"name": "db2", "error": 0.1}]),
        ToolResult(ok=True, content=[{"name": "db", "error": 0.1}, {}]),
        ToolResult(ok=True, content=[{"name": "db"}]),
        ToolResult(ok=False, content=left.content),
    ):
        assert not observations_match(left, right, 1e-12)
