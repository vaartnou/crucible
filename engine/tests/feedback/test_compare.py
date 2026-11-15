"""A failed or unpaired batch must never establish the model gap."""

import json
from pathlib import Path
from typing import Any

import pytest

from crucible.feedback.compare import compare


def batches(tmp_path: Path) -> tuple[Path, Path]:
    bots, agent = tmp_path / "bots", tmp_path / "agent"
    bots.mkdir()
    agent.mkdir()
    identity = {"task": "oncall@1", "salt_sha256": "salt", "simulator_sha256": "v1"}
    seeds = list(range(10))
    (bots / "gates.json").write_text(
        json.dumps(
            {
                **identity,
                "seeds": seeds,
                "scores": {"graph_walk": [0.9] * 10},
                "errors": [],
            }
        )
    )
    (agent / "manifest.json").write_text(
        json.dumps(
            {
                **identity,
                "model": "screening-model",
                "seeds": seeds,
            }
        )
    )
    (agent / "summary.json").write_text(
        json.dumps([{"seed": seed, "reward": 0.5, "stop_reason": "task_done"} for seed in seeds])
    )
    return bots, agent


def change(path: Path, key: str, value: Any) -> None:
    data = json.loads(path.read_text())
    data[key] = value
    path.write_text(json.dumps(data))


def test_mean_gap_does_not_replace_seven_seed_requirement(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    rows = json.loads((agent / "summary.json").read_text())
    for index, row in enumerate(rows):
        row["reward"] = 0.1 if index < 6 else 0.9
    (agent / "summary.json").write_text(json.dumps(rows))
    result = compare(bots, agent)
    assert result["mean_gap"] > 0.2
    assert result["seeds_with_gap_at_least_020"] == 6
    assert result["passed"] is False


def test_seven_seed_requirement_does_not_replace_mean_gap(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    rows = json.loads((agent / "summary.json").read_text())
    for index, row in enumerate(rows):
        row["reward"] = 0.65 if index < 7 else 1.0
    (agent / "summary.json").write_text(json.dumps(rows))
    result = compare(bots, agent)
    assert result["mean_gap"] < 0.2
    assert result["seeds_with_gap_at_least_020"] == 7
    assert result["passed"] is False


@pytest.mark.parametrize("reward", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_invalid_model_reward_rejected(tmp_path: Path, reward: Any) -> None:
    bots, agent = batches(tmp_path)
    rows = json.loads((agent / "summary.json").read_text())
    rows[0]["reward"] = reward
    (agent / "summary.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="finite numbers"):
        compare(bots, agent)


def test_duplicate_baseline_seeds_rejected(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "seeds", [0] * 10)
    with pytest.raises(ValueError, match="distinct integer"):
        compare(bots, agent)


def test_unpaired_seed_rejected(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "seeds", list(range(1, 11)))
    with pytest.raises(ValueError, match="paired graph_walk"):
        compare(bots, agent)


def test_manifest_seed_mismatch_rejected(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(agent / "manifest.json", "seeds", list(range(1, 11)))
    with pytest.raises(ValueError, match="manifest seeds"):
        compare(bots, agent)


def test_bot_environment_error_rejected(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "errors", ["graph_walk/3: RuntimeError"])
    with pytest.raises(ValueError, match="Bot environment errors"):
        compare(bots, agent)


def test_exact_twenty_point_gap_tolerates_float_roundoff(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "scores", {"graph_walk": [0.85] * 10})
    rows = json.loads((agent / "summary.json").read_text())
    for row in rows:
        row["reward"] = 0.65
    (agent / "summary.json").write_text(json.dumps(rows))
    assert compare(bots, agent)["passed"] is True
    for row in rows:
        row["reward"] = 0.650001
    (agent / "summary.json").write_text(json.dumps(rows))
    assert compare(bots, agent)["passed"] is False


def test_same_environment_allows_policy_only_changes_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "environment_sha256", "environment-v1")
    change(agent / "manifest.json", "environment_sha256", "environment-v1")
    change(bots / "gates.json", "simulator_sha256", "new-policy")
    result = compare(bots, agent)
    assert result["passed"] is True
    assert result["environment_sha256"] == "environment-v1"
    assert result["bot_simulator_sha256"] == "new-policy"
    assert result["agent_simulator_sha256"] == "v1"


def test_changed_environment_rejected_even_when_legacy_hash_matches(tmp_path: Path) -> None:
    bots, agent = batches(tmp_path)
    change(bots / "gates.json", "environment_sha256", "environment-v1")
    change(agent / "manifest.json", "environment_sha256", "environment-v2")
    with pytest.raises(ValueError, match="environment source hash"):
        compare(bots, agent)


@pytest.mark.parametrize("side", ["bots", "agent"])
def test_one_sided_environment_fingerprint_rejected(tmp_path: Path, side: str) -> None:
    bots, agent = batches(tmp_path)
    path = bots / "gates.json" if side == "bots" else agent / "manifest.json"
    change(path, "environment_sha256", "environment-v1")
    with pytest.raises(ValueError, match="Both batches"):
        compare(bots, agent)
