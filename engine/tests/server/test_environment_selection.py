"""Server imports must respect the deployment's environment selection."""

import os
import subprocess
import sys


def test_server_startup_keeps_unselected_environments_unregistered() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from crucible.server.app import create_app; "
            "from crucible.core.registry import list_tasks; "
            "create_app(); "
            "assert list_tasks() == ['oncall@1'], list_tasks()",
        ],
        env={**os.environ, "CRUCIBLE_ENVIRONMENTS": "oncall"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
