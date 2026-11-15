"""Brief test 8: an episode over HTTP /v1 equals the same job run in process."""

from pathlib import Path

import httpx
import pytest

from crucible.agents.remote import HttpEpisode, drive
from crucible.core.executor import LocalExecutor
from crucible.core.jobs import Job
from crucible.core.policy import Policy
from crucible.core.registry import get_policy, get_task
from crucible.core.transcript import TIME_FIELDS, read_transcript
from crucible.server.app import create_app
from crucible.server.settings import Settings

TASK = "guess@1"
POLICIES = ["reference", "do_nothing"]


@pytest.mark.parametrize("policy_name", POLICIES)
@pytest.mark.parametrize("seed", range(5))
async def test_api_episode(policy_name: str, seed: int, tmp_path: Path) -> None:
    local = (
        await LocalExecutor().run([Job(task=TASK, policy=policy_name, seed=seed)], tmp_path / "l")
    )[0]
    local_t = read_transcript(local.transcript_path)

    app = create_app(Settings(runs_dir=tmp_path / "srv"))
    transport = httpx.ASGITransport(app=app)
    episode, instruction = await HttpEpisode.start(
        TASK, seed, base_url="http://test", transport=transport
    )
    task = get_task(TASK)
    policy: Policy = get_policy(policy_name, TASK)
    outcome = await drive(
        policy, episode, task.spec, task.tools(), instruction, episode.first_observation
    )
    remote_t = read_transcript(tmp_path / "srv" / "episodes" / f"{episode.episode_id}.jsonl")

    assert outcome.score == local.score
    assert outcome.steps == local.steps
    assert outcome.stop_reason == local.stop_reason
    assert remote_t.steps == local_t.steps
    assert remote_t.footer.score == local_t.footer.score
    assert remote_t.footer.steps == local_t.footer.steps
    assert remote_t.footer.stop_reason == local_t.footer.stop_reason
    ignore = {*TIME_FIELDS, "policy", "job_id", "repeat"}
    a = local_t.header.model_dump(exclude=ignore)
    b = remote_t.header.model_dump(exclude=ignore)
    assert a == b
    assert remote_t.header.policy == "external"
