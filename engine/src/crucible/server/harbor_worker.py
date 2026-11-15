"""Authenticated Harbor forwarding between the public UI and a Docker-capable worker."""

import hmac
from urllib.parse import urlsplit

from httpx import AsyncClient, HTTPError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from crucible.server.settings import Settings


def _harbor_path(path: str) -> bool:
    return path.startswith("/ui/harbor/") or path in {"/ui/runs", "/ui/runs/replay"}


class HarborWorkerMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = self.settings
        if settings.harbor_worker_mode:
            if request.url.path == "/healthz":
                return await call_next(request)
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {settings.harbor_worker_token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse({"detail": "Worker authentication required"}, status_code=401)
            if not _harbor_path(request.url.path):
                return JSONResponse({"detail": "Worker route not available"}, status_code=404)
            return await call_next(request)

        if not settings.harbor_worker_url or not _harbor_path(request.url.path):
            return await call_next(request)
        if request.method not in {"GET", "POST"}:
            return JSONResponse({"detail": "Method not allowed"}, status_code=405)
        # Never forward browser credentials, cookies, hosts, or provider URLs to the worker.
        headers = {"Authorization": f"Bearer {settings.harbor_worker_token}"}
        if "content-type" in request.headers:
            headers["Content-Type"] = request.headers["content-type"]
        try:
            async with AsyncClient(timeout=120, follow_redirects=False, trust_env=False) as client:
                response = await client.request(
                    request.method,
                    settings.harbor_worker_url.rstrip("/") + request.url.path,
                    params=request.url.query,
                    content=await request.body(),
                    headers=headers,
                )
        except HTTPError:
            return JSONResponse(
                {"detail": "Harbor worker unavailable. Check its connection and runtime."},
                status_code=503,
            )
        if 300 <= response.status_code < 400:
            return JSONResponse({"detail": "Unexpected worker redirect"}, status_code=502)
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )


def validate_worker_settings(settings: Settings) -> None:
    if settings.harbor_worker_mode and settings.harbor_worker_url:
        raise ValueError("Set HARBOR_WORKER_MODE on the worker or HARBOR_WORKER_URL on the UI")
    if (settings.harbor_worker_mode or settings.harbor_worker_url) and len(
        settings.harbor_worker_token
    ) < 32:
        raise ValueError("HARBOR_WORKER_TOKEN must contain at least 32 characters")
    if settings.harbor_worker_url:
        url = urlsplit(settings.harbor_worker_url)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"} or (
            url.hostname is not None and url.hostname.endswith(".railway.internal")
        )
        if (
            not url.hostname
            or (url.scheme != "https" and not (url.scheme == "http" and local))
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "HARBOR_WORKER_URL must be an HTTPS origin or a private/local HTTP origin"
            )
