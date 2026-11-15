"""Explicitly bounded numeric comparison for Linux/macOS float roundoff in replays."""

import math

from pydantic import JsonValue

from crucible.core.tools import ToolResult

MAX_NUMERIC_TOLERANCE = 1e-12


def observations_match(left: ToolResult, right: ToolResult, tolerance: float = 0) -> bool:
    if not 0 <= tolerance <= MAX_NUMERIC_TOLERANCE:
        raise ValueError("Numeric replay tolerance must be between zero and 1e-12")

    def same(a: JsonValue, b: JsonValue) -> bool:
        if a == b:
            return True
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(same(value, b[key]) for key, value in a.items())
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
        if isinstance(a, float) and isinstance(b, float):
            return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)
        return False

    return left.ok == right.ok and left.error == right.error and same(left.content, right.content)
