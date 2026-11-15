"""Independent application used to prove the HTTP backend boundary.

This is deliberately a small fixture, not Fleetbase or a benchmark. The deployment
runner owns its process. Every mutation is scoped to an episode-owned resource.
"""

import hmac
import os
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = "delivery-1"
    status: Literal["available", "dispatched", "delivered"] = "available"


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["delivered"]


def create_app() -> FastAPI:
    token = os.environ.get("BACKEND_PROBE_TOKEN", "")
    if not token:
        raise ValueError("BACKEND_PROBE_TOKEN is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    records: dict[str, Record] = {}

    async def authorize(x_backend_token: Annotated[str | None, Header()] = None) -> None:
        if x_backend_token is None or not hmac.compare_digest(x_backend_token, token):
            raise HTTPException(403, "Forbidden")

    @app.get("/healthz")
    async def health() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ready": True}

    @app.put("/episodes/{episode_id}", dependencies=[Depends(authorize)])
    async def prepare(episode_id: str) -> Record:  # pyright: ignore[reportUnusedFunction]
        return records.setdefault(episode_id, Record())

    @app.get("/episodes/{episode_id}", dependencies=[Depends(authorize)])
    async def snapshot(episode_id: str) -> Record:  # pyright: ignore[reportUnusedFunction]
        if episode_id not in records:
            raise HTTPException(404, "Episode not found")
        return records[episode_id]

    @app.post("/episodes/{episode_id}/dispatch", dependencies=[Depends(authorize)])
    async def dispatch(episode_id: str) -> Record:  # pyright: ignore[reportUnusedFunction]
        record = await snapshot(episode_id)
        if record.status == "available":
            record.status = "dispatched"
        return record

    @app.put("/episodes/{episode_id}/progress", dependencies=[Depends(authorize)])
    async def progress(  # pyright: ignore[reportUnusedFunction]
        episode_id: str, body: Progress
    ) -> Record:
        record = await snapshot(episode_id)
        if record.status == "available":
            raise HTTPException(409, "Order is not dispatched")
        record.status = body.status
        return record

    @app.delete("/episodes/{episode_id}", dependencies=[Depends(authorize)])
    async def release(episode_id: str) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        records.pop(episode_id, None)
        return {"released": True}

    return app
