"""Execute the exported verifier's Python entrypoint without Docker or a live sidecar."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

TEMPLATE = Path(__file__).parents[2] / "src/crucible/export/templates/harbor/tests/test.sh"


def run_verifier(body: dict[str, object] | None) -> dict[str, str]:
    script = TEMPLATE.read_text().split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    with TemporaryDirectory() as directory:
        script = script.replace('out = "/logs/verifier"', f"out = {directory!r}")
        response = io.BytesIO(json.dumps(body).encode())
        with (
            patch(
                "urllib.request.urlopen",
                return_value=response,
                side_effect=OSError("sidecar unavailable") if body is None else None,
            ),
            patch("time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            exec(compile(script, str(TEMPLATE), "exec"), {})
        return {path.name: path.read_text() for path in Path(directory).iterdir()}


class TestVerifierReward(unittest.TestCase):
    def test_partial_reward_and_diagnostics(self) -> None:
        body: dict[str, object] = {
            "reward": 0.735,
            "breakdown": {"impact": 0.9, "penalty": 0.1},
            "flags": {"verified": True},
            "steps": 12,
            "stop_reason": "resolved",
            "transcript": [{"tool": "resolve"}],
        }
        files = run_verifier(body)
        self.assertEqual(set(files), {"reward.txt", "grade.json", "transcript.json"})
        self.assertEqual(float(files["reward.txt"]), 0.735)
        grade = json.loads(files["grade.json"])
        self.assertEqual(grade["breakdown"], body["breakdown"])
        self.assertEqual(grade["flags"], body["flags"])
        self.assertEqual(json.loads(files["transcript.json"]), body["transcript"])

    def test_score_bounds_and_nonfinite_values(self) -> None:
        for reward, expected in (
            (-1.0, 0.0),
            (0.0, 0.0),
            (1.0, 1.0),
            (2.0, 1.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
            (-float("inf"), 0.0),
        ):
            with self.subTest(reward=reward):
                files = run_verifier(
                    {
                        "reward": reward,
                        "breakdown": {},
                        "flags": {},
                        "steps": 0,
                        "stop_reason": "resolved",
                        "transcript": [],
                    }
                )
                self.assertEqual(float(files["reward.txt"]), expected)
                self.assertNotIn("reward.json", files)

    def test_unavailable_sidecar_writes_zero_and_error(self) -> None:
        files = run_verifier(None)
        self.assertEqual(set(files), {"reward.txt", "grade_error.txt"})
        self.assertEqual(float(files["reward.txt"]), 0.0)
        self.assertIn("sidecar unavailable", files["grade_error.txt"])
