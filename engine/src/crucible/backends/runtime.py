"""Application-independent connections and exclusive episode leases.

Services belong to the deployment runner. This module owns only an episode's
connection and lease. See ``docs/backend-runtime.md`` for the exclusivity boundary.
"""

import asyncio
import fcntl
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from crucible.core.episode import InfraError


class BackendError(RuntimeError):
    """A backend failed without exposing connection details or response contents."""


class BackendUnavailable(BackendError, InfraError):
    """A transient failure before the episode starts; the executor retries it."""


class LeaseConflict(BackendUnavailable):
    """Another episode already owns the deployment."""


class BackendConnection(BaseModel):
    """Private configuration. Do not put this object in observations or tool schemas.

    ``deployment_id`` must name the installation, consistently across all URL
    aliases and cooperating processes. ``exclusive_access`` is the deployment
    owner's assertion that no other clients can modify the installation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    deployment_id: str = Field(min_length=1, repr=False)
    base_url: SecretStr = Field(repr=False)
    headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    ownership: Literal["attached", "managed"] = "attached"
    exclusive_access: bool = False
    timeout_seconds: float = Field(default=10, gt=0)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: SecretStr) -> SecretStr:
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("backend URL must use HTTP or HTTPS")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("backend URL must not contain credentials, query, or fragment")
        return value


@dataclass(frozen=True)
class ResourceLease:
    """Identifies resources owned by exactly one episode.

    An external isolation provider may supply a dedicated namespace and release
    it server-side. The default provider reserves the entire installation.
    """

    episode_id: str
    namespace: str
    isolation: Literal["exclusive", "isolated"]
    release: Callable[[], Awaitable[None]] = field(repr=False, compare=False)


class LeaseProvider(Protocol):
    """Implementations must enforce isolation before returning a lease.

    Namespace providers must allocate and verify a separate application tenant or
    database, not merely attach an episode label to shared business records.
    """

    async def acquire(self, connection: BackendConnection, episode_id: str) -> ResourceLease: ...


class ExclusiveLeaseProvider:
    """Nonblocking, process-safe leases for a deployment with exclusive tenancy.

    All cooperating processes must share ``lock_dir`` and ``deployment_id``.
    Never unlink lock files: doing so allows two live locks on different inodes.
    """

    def __init__(self, lock_dir: Path) -> None:
        self._lock_dir = lock_dir

    async def acquire(self, connection: BackendConnection, episode_id: str) -> ResourceLease:
        if not connection.exclusive_access:
            raise BackendError("deployment requires proven isolation or exclusive access")
        self._lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(connection.deployment_id.encode()).hexdigest()
        descriptor = os.open(self._lock_dir / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise LeaseConflict("deployment is already leased by another episode") from None
        except BaseException:
            os.close(descriptor)
            raise
        released = False

        async def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

        return ResourceLease(episode_id, "exclusive", "exclusive", release)


@dataclass
class BackendEpisode:
    """Private adapter context. Keep the client and lease out of public results."""

    resource: ResourceLease
    client: httpx.AsyncClient = field(repr=False)


type EpisodeHook = Callable[[BackendEpisode], Awaitable[None]]
type ArtifactCollector = Callable[[BackendEpisode], Awaitable[Mapping[str, bytes]]]


class BackendRuntime(Protocol):
    """Runtime operations available to an environment adapter."""

    async def acquire(self) -> BackendEpisode: ...

    async def ready(self) -> None: ...

    async def collect_artifacts(self, directory: Path) -> dict[str, str]: ...

    async def aclose(self) -> None: ...


class HttpBackendRuntime:
    """Acquire a lease, wait for HTTP readiness, then run adapter hooks.

    This object is single-use. ``prepare`` seeds only episode-owned resources;
    ``release`` removes only those resources and must tolerate partial preparation.
    Neither hook may start/stop services or reset an attached installation wholesale.
    """

    def __init__(
        self,
        connection: BackendConnection,
        leases: LeaseProvider,
        *,
        readiness_path: str = "/healthz",
        readiness_attempts: int = 30,
        readiness_interval: float = 0.2,
        prepare: EpisodeHook | None = None,
        release: EpisodeHook | None = None,
        artifacts: ArtifactCollector | None = None,
    ) -> None:
        if readiness_attempts < 1 or readiness_interval < 0:
            raise ValueError("readiness attempts must be positive and interval nonnegative")
        _relative_path(readiness_path)
        self._connection = connection
        self._leases = leases
        self._readiness_path = readiness_path
        self._attempts = readiness_attempts
        self._interval = readiness_interval
        self._prepare = prepare
        self._release = release
        self._artifacts = artifacts
        self._episode: BackendEpisode | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._cleanup_errors: list[str] = []
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        """Exception type names only; never response bodies or connection secrets."""
        return tuple(self._cleanup_errors)

    async def acquire(self) -> BackendEpisode:
        async with self._lock:
            if self._closed:
                raise BackendError("backend runtime is closed")
            if self._episode is not None:
                return self._episode
            # No HTTP calls or seed hooks happen before isolation is established.
            resource = await self._leases.acquire(self._connection, uuid4().hex)
            try:
                client = httpx.AsyncClient(
                    base_url=self._connection.base_url.get_secret_value().rstrip("/") + "/",
                    headers={k: v.get_secret_value() for k, v in self._connection.headers.items()},
                    timeout=self._connection.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                )
            except BaseException:
                # Client construction can fail before an episode context exists.
                # Releasing a custom provider's lease must not mask that failure.
                try:
                    await resource.release()
                except (Exception, asyncio.CancelledError) as cleanup_error:
                    name = type(cleanup_error).__name__
                    self._cleanup_errors.append(name)
                    logging.getLogger(__name__).error("backend cleanup failed: %s", name)
                raise
            self._episode = BackendEpisode(resource, client)
            try:
                await self.ready()
                if self._prepare is not None:
                    await self._prepare(self._episode)
            except BaseException:
                await self._close_unlocked()
                raise
            return self._episode

    async def ready(self) -> None:
        episode = self._episode
        if episode is None or self._closed:
            raise BackendError("backend has no active episode lease")
        for attempt in range(self._attempts):
            try:
                async with asyncio.timeout(self._connection.timeout_seconds):
                    response = await episode.client.get(self._readiness_path.lstrip("/"))
                    if response.is_success:
                        return
            except httpx.HTTPError, TimeoutError:
                pass
            if attempt + 1 < self._attempts:
                await asyncio.sleep(self._interval)
        raise BackendUnavailable("backend readiness check failed")

    async def collect_artifacts(self, directory: Path) -> dict[str, str]:
        """Write private, sanitized evidence and return relative-name SHA256 hashes.

        Collectors must return episode-owned evidence, not secrets or complete
        database dumps. Configured header secrets are additionally redacted here.
        """
        async with self._lock:
            episode = self._episode
            if episode is None or self._closed:
                raise BackendError("backend has no active episode lease")
            if self._artifacts is None:
                return {}
            artifacts = await self._artifacts(episode)
            # Validate all names before writing any artifacts.
            for name in artifacts:
                _artifact_name(name)
            secrets = tuple(
                v.get_secret_value().encode() for v in self._connection.headers.values()
            )
            return await asyncio.to_thread(_write_artifacts, directory, artifacts, secrets)

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        self._closed = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._release_episode())
        # A cancellation cannot strand a live lock or skip the HTTP client close.
        try:
            await asyncio.shield(self._cleanup_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup_task)
            raise

    async def _release_episode(self) -> None:
        episode = self._episode
        if episode is None:
            return
        # Try every release stage even when an adapter hook fails. Preserve primary
        # exceptions in acquire(), and expose sanitized cleanup diagnostics.
        operations: list[Callable[[], Awaitable[None]]] = []
        if self._release is not None:
            release = self._release
            operations.append(lambda: release(episode))
        operations.extend((episode.client.aclose, episode.resource.release))
        for operation in operations:
            try:
                await operation()
            except (Exception, asyncio.CancelledError) as exc:
                name = type(exc).__name__
                self._cleanup_errors.append(name)
                logging.getLogger(__name__).error("backend cleanup failed: %s", name)


def _write_artifacts(
    directory: Path, artifacts: Mapping[str, bytes], secrets: tuple[bytes, ...]
) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = directory.resolve()
    # Validate every destination before writing. Existing symlinks may not escape.
    for name in artifacts:
        if not (directory / name).resolve().is_relative_to(root):
            raise BackendError("artifact path escapes evidence directory")
    hashes: dict[str, str] = {}
    for name, content in artifacts.items():
        target = directory / name
        for secret in secrets:
            if secret:
                content = content.replace(secret, b"[REDACTED]")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(content)
        target.chmod(0o600)
        hashes[name] = hashlib.sha256(content).hexdigest()
    return hashes


def _relative_path(value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or parts.fragment or ".." in value.split("/"):
        raise ValueError("readiness path must be a local HTTP path")


def _artifact_name(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
        or value in {".", ".."}
    ):
        raise BackendError("artifact name must be a safe relative path")
