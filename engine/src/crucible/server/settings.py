"""Server settings, read from the environment (no prefix: `PORT`, `LOG_LEVEL`, ...)."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Environment-driven settings. Field names match env names, case-insensitively."""

    mode: Literal["api", "sidecar"] = "api"
    port: int = 8000
    log_level: str = "info"
    runs_dir: Path = Path("runs/server")
    session_ttl_seconds: int = 3600
    grade_token: str = ""
    solution_token: str = ""
    solution_policy: str = "reference"
    sidecar_task: str = "oncall@1"
    sidecar_seed: int = 0
    sidecar_salt: str | None = None


__all__ = ["Settings"]
