"""Server settings, read from the environment (no prefix: `PORT`, `LOG_LEVEL`, ...)."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Environment-driven settings. Field names match env names, case-insensitively."""

    mode: Literal["api", "sidecar"] = "api"
    port: int = 8000
    log_level: str = "info"
    runs_dir: Path = Path("runs/server")
    harbor_jobs_dir: Path = Path("runs/harbor-jobs")
    harbor_exports_dir: Path = Path("runs")
    harbor_worker_url: str = ""
    harbor_worker_token: str = Field(default="", repr=False)
    harbor_worker_mode: bool = False
    harbor_allowed_models: str = ""
    harbor_max_concurrency: int = Field(default=4, ge=1, le=4)
    harbor_daily_job_limit: int = Field(default=0, ge=0)
    session_ttl_seconds: int = 3600
    grade_token: str = ""
    solution_token: str = ""
    solution_policy: str = "reference"
    sidecar_task: str = "oncall@1"
    sidecar_seed: int = 0
    sidecar_salt: str | None = None
    spectator_path: Path | None = None
    spectator_run_id: str = ""
    spectator_strategy: str = "external"


__all__ = ["Settings"]
