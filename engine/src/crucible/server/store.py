"""Session store with asynchronous expiration and resource release."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from crucible.core.session import EpisodeSession

logger = logging.getLogger(__name__)


@dataclass
class Entry:
    """One live episode. The lock allows one HTTP call at a time."""

    session: EpisodeSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore(Protocol):
    def get(self, episode_id: str) -> Entry | None: ...

    def put(self, episode_id: str, entry: Entry) -> None: ...

    def delete(self, episode_id: str) -> None: ...

    async def expire(self) -> None: ...

    async def aclose(self) -> None: ...


class InMemorySessionStore:
    """Sliding TTL; retired entries remain owned until asynchronous cleanup completes."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, tuple[Entry, float]] = {}
        self._retired: list[Entry] = []
        self._cleanup_lock = asyncio.Lock()

    def get(self, episode_id: str) -> Entry | None:
        item = self._items.get(episode_id)
        if item is None:
            return None
        entry, last_used = item
        now = self._clock()
        if now - last_used >= self._ttl and not entry.lock.locked():
            self.delete(episode_id)
            return None
        self._items[episode_id] = (entry, now)
        return entry

    def put(self, episode_id: str, entry: Entry) -> None:
        if episode_id in self._items:
            raise ValueError("Episode already exists")
        self._items[episode_id] = (entry, self._clock())

    def delete(self, episode_id: str) -> None:
        item = self._items.pop(episode_id, None)
        if item is not None:
            self._retired.append(item[0])

    async def expire(self) -> None:
        """Expire idle sessions, including sessions with no subsequent HTTP traffic."""
        now = self._clock()
        for episode_id, (entry, last_used) in list(self._items.items()):
            if now - last_used >= self._ttl and not entry.lock.locked():
                self.delete(episode_id)
        await self._drain()

    async def aclose(self) -> None:
        """Release all sessions on server shutdown."""
        for episode_id in list(self._items):
            self.delete(episode_id)
        await self._drain()

    async def _drain(self) -> None:
        async with self._cleanup_lock:
            while self._retired:
                entry = self._retired[0]
                try:
                    async with entry.lock:
                        await entry.session.close()
                except Exception:
                    logger.exception("Failed to finalize retired episode")
                # Leave ownership in the queue if this coroutine is cancelled.
                self._retired.pop(0)


__all__ = ["Entry", "InMemorySessionStore", "SessionStore"]
