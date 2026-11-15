"""Harbor is the only model path; jobs and transcripts persist outside UI sessions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException

from crucible.server.app import create_app
from crucible.server.harbor_jobs import HarborJobs, LaunchHarborRequest
from crucible.server.settings import Settings


def prepare_export(root: Path) -> None:
    task = root / "oncall-export" / "oncall-1-seed-0"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[metadata]\ntask_ref = "oncall@1"\nseed = 0\n')


def test_overlap_exports_are_available_to_watch(tmp_path: Path) -> None:
    prepare_export(tmp_path)
    task = tmp_path / "overlap-export" / "oncall-2-seed-0"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[metadata]\ntask_ref = "oncall@2"\nseed = 0\n')
    jobs = HarborJobs(Settings(harbor_exports_dir=tmp_path))
    assert {row["name"] for row in jobs.exports()} == {"oncall-export", "overlap-export"}


async def test_direct_model_sessions_are_rejected(tmp_path: Path) -> None:
    app = create_app(Settings(runs_dir=tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/ui/sessions", json={"mode": "llm"})
        assert response.status_code == 422


@pytest.mark.parametrize(
    "configured_key", ["", "GOOGLE_GENERATIVE_AI_API_KEY", "CUSTOM_MODEL_API_KEY"]
)
async def test_launch_uses_harbor_opencode_docker_and_durable_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_key: str
) -> None:
    prepare_export(tmp_path / "exports")
    settings = Settings(
        harbor_jobs_dir=tmp_path / "jobs",
        harbor_exports_dir=tmp_path / "exports",
        harbor_api_key_name=configured_key,
    )
    monkeypatch.setenv("GOOGLE_GENERATIVE_AI_API_KEY", "secret-test-key-value")
    jobs = HarborJobs(settings)
    monkeypatch.setattr(
        "crucible.server.harbor_jobs.shutil.which", Mock(return_value="/bin/harbor")
    )
    monkeypatch.setattr(
        "crucible.server.harbor_jobs.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess[bytes](args=[], returncode=0)),
    )
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr("crucible.server.harbor_jobs.subprocess.Popen", launch)
    row = await jobs.launch(LaunchHarborRequest(export="oncall-export"))
    command = launch.call_args.args[0]
    assert command[:2] == ["/bin/harbor", "run"]
    for flag, value in [
        ("--agent", "opencode"),
        ("--env", "docker"),
        ("--model", "google/gemini-3.8-flash"),
        ("--jobs-dir", str(settings.harbor_jobs_dir)),
        ("--job-name", row["id"]),
        ("--api-key-name", configured_key or "GOOGLE_GENERATIVE_AI_API_KEY"),
    ]:
        assert command[command.index(flag) + 1] == value
    assert "--base-url" not in command
    assert "secret-test-key-value" not in command
    assert command.count("--api-key-name") == 1
    with pytest.raises(HTTPException) as duplicate:
        await jobs.launch(LaunchHarborRequest(export="oncall-export"))
    assert duplicate.value.status_code == 409
    folder = Path(row["path"])
    assert (folder / "crucible-launch.json").is_file()
    (folder / "result.json").write_text(
        json.dumps(
            {
                "finished_at": "done",
                "stats": {"n_completed_trials": 1, "n_errored_trials": 0},
                "n_total_trials": 1,
            }
        )
    )
    restarted = HarborJobs(settings)
    saved = restarted.describe()["jobs"][0]
    assert saved["status"] == "completed"
    assert saved["agent"] == "opencode"
    assert saved["completed_trials"] == 1


async def test_launch_rejects_unlisted_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_export(tmp_path / "exports")
    jobs = HarborJobs(
        Settings(harbor_jobs_dir=tmp_path / "jobs", harbor_exports_dir=tmp_path / "exports")
    )
    monkeypatch.setattr(
        "crucible.server.harbor_jobs.shutil.which", Mock(return_value="/bin/harbor")
    )
    for export in ["../exports", "/tmp", "missing"]:
        with pytest.raises(HTTPException) as invalid:
            await jobs.launch(LaunchHarborRequest(export=export))
        assert invalid.value.status_code == 400
    assert not (tmp_path / "jobs").exists()


async def test_runs_reads_only_configured_harbor_directory(tmp_path: Path) -> None:
    from crucible.core.scoring import Score
    from crucible.core.transcript import Footer, Header

    jobs_dir = tmp_path / "jobs"
    trial = jobs_dir / "saved-job" / "trial" / "verifier"
    trial.mkdir(parents=True)
    header = Header(
        task="oncall@1",
        scoring_hash="test",
        policy="opencode",
        seed=0,
        repeat=0,
        job_id="test",
        git_commit="test",
        started_at="test",
    )
    footer = Footer(steps=0, stop_reason="policy_stop", score=Score(total=0.5), ended_at="test")
    (trial / "transcript.json").write_text(
        json.dumps([header.model_dump(mode="json"), footer.model_dump(mode="json")])
    )
    local = tmp_path / "manual"
    local.mkdir()
    (local / "old-model.jsonl").write_text(
        header.model_dump_json() + "\n" + footer.model_dump_json() + "\n"
    )
    app = create_app(
        Settings(runs_dir=local, harbor_jobs_dir=jobs_dir, harbor_exports_dir=tmp_path / "exports")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        runs = (await client.get("/ui/runs")).json()
        assert len(runs) == 1
        assert runs[0]["source_kind"] == "harbor"
        assert runs[0]["group"] == "saved-job"
        assert runs[0]["score"] == 0.5
