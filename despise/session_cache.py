"""Per-token gateway session cache (optional, ``cashews``-backed).

Why this exists: Discord only lets a client *resume* a gateway session when it
still holds that session's ``session_id`` and last ``seq``. Lose them and the
next connection must send a fresh Identify — and Identify is rate limited per
token (close 4008, plus a daily cap). A flapping connection with no session
memory therefore burns the identify budget and eventually locks the account
out, so the session has to outlive the socket that produced it.

State is stored under one key per token::

    despise:gw:session:{token} -> {"session_id": ..., "seq": ..., "resume_url": ...}

Keying by token (not by user id) matters: the same account can have several
tokens, and swapping a token under a user id must never resurrect the wrong
session.

Backends are pluggable:

- ``mem://`` (default) -- in-memory only. Isolates tokens from each other,
  costs no disk I/O, and dies with the process.
- ``redis://redis/0`` -- survives a process restart and is shared across
  workers. This is the only backend that actually delivers "resume after a
  restart"; with ``mem://`` the cache still helps *within* one process
  (multiple clients sharing a token, a client rebuilt in-place).

cashews is an optional dependency::

    pip install despise[session-cache]         # mem://
    pip install despise[session-cache-redis]   # redis://
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("despise.session_cache")

KEY_PREFIX = "despise:gw:session"

# Discord invalidates sessions server-side on a long enough gap. An hour is
# generous enough to cover a restart while stopping an abandoned token from
# resuming a week-old session.
DEFAULT_TTL_S = 3600.0

# Saves are throttled: seq advances on *every* dispatch, and writing a cache
# entry per message would be absurd. READY/RESUMED use force=True.
DEFAULT_MIN_SAVE_INTERVAL_S = 30.0

_INSTALL_HINT = (
    "session cache requires cashews; install it with "
    "`pip install despise[session-cache]` (or `despise[session-cache-redis]`)"
)


def _load_cache_class() -> Any:
    try:
        from cashews import Cache
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(_INSTALL_HINT) from exc
    return Cache


@dataclass(slots=True)
class SessionState:
    """A resumable gateway session."""

    session_id: str
    seq: int | None = None
    resume_url: str | None = None


class SessionCache:
    """Stores one gateway session per token.

    Writes are rate-limited by ``min_save_interval`` unless ``force=True``.
    Reads never raise: a cache problem must not take the gateway down, so a
    failed load degrades to "no session, re-identify".
    """

    def __init__(
        self,
        token: str,
        *,
        backend: str = "mem://",
        ttl: float = DEFAULT_TTL_S,
        min_save_interval: float = DEFAULT_MIN_SAVE_INTERVAL_S,
        name: str | None = None,
    ) -> None:
        cache_cls = _load_cache_class()
        self.token = token
        self.key = f"{KEY_PREFIX}:{token}"
        self.ttl = ttl
        self.min_save_interval = min_save_interval
        self._last_save = 0.0
        self._closed = False
        # A distinct name per token: cashews instances do not share state,
        # which is exactly the isolation we want between tokens.
        # Use hashlib for a stable hash that's consistent across runs.
        token_hash = hashlib.md5(token.encode("utf-8")).hexdigest()
        self._cache = cache_cls(name=name or f"despise-gw-{token_hash}")
        self._cache.setup(backend)  # sync in cashews 7.x -- do NOT await

    async def load(self) -> SessionState | None:
        if self._closed:
            return None
        try:
            raw = await self._cache.get(self.key)
        except Exception as exc:
            log.warning("session cache read failed, re-identifying: %r", exc)
            return None
        if not isinstance(raw, dict):
            return None
        session_id = raw.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        seq = raw.get("seq")
        return SessionState(
            session_id=session_id,
            seq=seq if isinstance(seq, int) else None,
            resume_url=raw.get("resume_url"),
        )

    async def save(
        self,
        session_id: str,
        seq: int | None = None,
        resume_url: str | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Persist the session. Returns True if a write actually happened."""
        if self._closed or not session_id:
            return False
        now = time.monotonic()
        if not force and self._last_save and now - self._last_save < self.min_save_interval:
            return False
        try:
            payload = {
                "session_id": session_id,
                "seq": seq,
                "resume_url": resume_url,
            }
            await self._cache.set(self.key, payload, expire=self.ttl)
        except Exception as exc:
            log.warning("session cache write failed: %r", exc)
            return False
        self._last_save = now
        return True

    async def clear(self) -> None:
        """Forget the session, so the next connect re-identifies."""
        if self._closed:
            return
        try:
            await self._cache.delete(self.key)
        except Exception as exc:
            log.debug("session cache delete failed: %r", exc)

    async def close(self) -> None:
        self._closed = True
        try:
            await self._cache.close()
        except Exception:
            log.debug("cache close failed", exc_info=True)


_shared: dict[tuple[str, str], SessionCache] = {}


def for_token(
    token: str,
    *,
    backend: str = "mem://",
    ttl: float = DEFAULT_TTL_S,
    name: str | None = None,
) -> SessionCache:
    """Get-or-create the cache for ``token``.

    Two clients using the same token in one process get the *same* cache, so
    they can hand a live session to each other. Different tokens never share.
    """
    shared_key = (token, backend)
    if (found := _shared.get(shared_key)) is not None and not found._closed:
        return found
    created = SessionCache(token, backend=backend, ttl=ttl, name=name)
    _shared[shared_key] = created
    return created


def forget(cache: SessionCache) -> None:
    """Drop a cache from the shared registry (after closing it)."""
    for key, value in list(_shared.items()):
        if value is cache:
            del _shared[key]
