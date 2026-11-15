"""Policy scores are pinned per (task ref, policy, seed); a change needs a task version bump."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.core.registry import get_task, list_tasks

GOLDEN = Path(__file__).parent / "golden_scores.json"
SEEDS = list(range(10))
# The legacy oncall@1 exemption remains; oncall@2 has pinned regression scores.
POLICIES: dict[str, list[str]] = {
    ref: ["reference", *type(get_task(ref)).checks.exploit_policies, "do_nothing"]
    for ref in list_tasks()
    if ref != "oncall@1"
}
CASES = [(task, policy) for task, names in POLICIES.items() for policy in names]
MESSAGE = (
    "Score changed: bump the task version or, if the change is intended for this version, "
    "regenerate with CRUCIBLE_UPDATE_GOLDEN=1."
)

Scores = dict[str, dict[str, dict[str, float]]]


@pytest.fixture(scope="module")
def measured(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Scores:
    cases = CASES
    if os.environ.get("CRUCIBLE_UPDATE_GOLDEN") != "1":
        # A focused check must not rerun unrelated environments. Full collection still
        # measures every case; explicit golden regeneration always measures every case.
        cases = [
            (str(item.callspec.params["task"]), str(item.callspec.params["policy"]))
            for item in request.session.items
            if isinstance(item, pytest.Function)
            and item.path == Path(__file__)
            and hasattr(item, "callspec")
            and "task" in item.callspec.params
            and "policy" in item.callspec.params
        ]
    jobs = [Job(task=t, policy=p, seed=s) for t, p in cases for s in SEEDS]
    out = tmp_path_factory.mktemp("golden")
    results = asyncio.run(LocalExecutor(concurrency=8).run(jobs, out))
    scores: Scores = {}
    for r in results:
        scores.setdefault(r.job.task, {}).setdefault(r.job.policy, {})[str(r.seed)] = r.score.total
    return scores


@pytest.fixture(scope="module")
def golden(request: pytest.FixtureRequest) -> Scores:
    if os.environ.get("CRUCIBLE_UPDATE_GOLDEN") == "1":
        measured: Scores = request.getfixturevalue("measured")
        GOLDEN.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
    return json.loads(GOLDEN.read_text())


def test_every_registered_task_has_golden_scores(golden: Scores) -> None:
    missing = [ref for ref in POLICIES if ref not in golden]
    assert not missing, f"No golden scores for {missing}: regenerate with CRUCIBLE_UPDATE_GOLDEN=1."


@pytest.mark.parametrize(("task", "policy"), CASES)
def test_scores_match_golden(measured: Scores, golden: Scores, task: str, policy: str) -> None:
    if task not in golden:
        pytest.skip(f"{task} has no golden scores yet (see the test above)")
    expected = golden[task][policy]
    actual = measured[task][policy]
    assert actual.keys() == expected.keys()
    for seed, score in actual.items():
        where = f"{task} {policy} seed {seed}"
        assert score == pytest.approx(expected[seed], abs=1e-9), f"{where}. {MESSAGE}"
