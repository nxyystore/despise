"""Per-token gateway session cache (built in, zero dependencies).

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

Backends are built in (no optional extras):

- ``mem://`` (default) -- in-memory only. Isolates tokens from each other,
  costs no disk I/O, and dies with the process. ``mem://?size=N`` caps the
  number of entries (default 1000).
- ``redis://host:port/db`` (and ``rediss://`` for TLS) -- survives a process
  restart and is shared across workers, over a minimal stdlib-asyncio RESP
  client. This is the only backend that actually delivers "resume after a
  restart"; with ``mem://`` the cache still helps *within* one process
  (multiple clients sharing a token, a client rebuilt in-place).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import ssl
import time
import urllib.parse
from collections import OrderedDict
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

DEFAULT_MEM_SIZE = 1000


def _parse_mem_size(backend: str) -> int:
    try:
        query = urllib.parse.urlsplit(backend).query
        size = int(urllib.parse.parse_qs(query).get("size", [DEFAULT_MEM_SIZE])[0])
        return size if size > 0 else DEFAULT_MEM_SIZE
    except (ValueError, TypeError):
        return DEFAULT_MEM_SIZE


class _MemoryBackend:
    """Async dict with per-key TTL and an LRU-ish size cap."""

    def __init__(self, max_size: int = DEFAULT_MEM_SIZE) -> None:
        self._data: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if expires_at is not None and time.monotonic() >= expires_at:
                del self._data[key]
                return default
            # LRU touch so hot sessions survive the size cap.
            self._data.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, expire: float | None = None, **_: Any) -> None:
        # Accept `ttl` as an alias for cashews-compat callers.
        expires_at = time.monotonic() + expire if expire else None
        async with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (value, expires_at)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def close(self) -> None:
        async with self._lock:
            self._data.clear()


class _RedisBackend:
    """Minimal stdlib asyncio Redis client (GET/SET/DEL only).

    Opens a short-lived connection per operation: session saves are throttled
    to ~1/30s, so connection setup cost is negligible and we avoid reconnect
    state machines. Any failure raises, and the caller degrades to
    "no session, re-identify".
    """

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("redis", "rediss"):
            raise ValueError(f"unsupported redis URL: {url!r}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 6379
        self.db = 0
        if parsed.path and parsed.path != "/":
            try:
                self.db = int(parsed.path.lstrip("/"))
            except ValueError:
                self.db = 0
        self.password = parsed.password
        self.ssl_context = ssl.create_default_context() if parsed.scheme == "rediss" else None
        self._selected = self.db == 0 and self.password is None

    @staticmethod
    def _encode(*args: Any) -> bytes:
        parts = [f"*{len(args)}\r\n".encode()]
        for arg in args:
            raw = arg if isinstance(arg, bytes) else str(arg).encode()
            parts += [f"${len(raw)}\r\n".encode(), raw, b"\r\n"]
        return b"".join(parts)

    async def _roundtrip(self, *args: Any) -> Any:
        reader, writer = await asyncio.open_connection(
            self.host, self.port, ssl=self.ssl_context
        )
        try:
            if self.password:
                await self._command(reader, writer, "AUTH", self.password)
            if self.db:
                await self._command(reader, writer, "SELECT", self.db)
            return await self._command(reader, writer, *args)
        finally:
            writer.close()
            with contextlib.suppress(Exception):  # pragma: no cover - best effort
                await writer.wait_closed()

    async def _command(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *args: Any) -> Any:
        writer.write(self._encode(*args))
        await writer.drain()
        return await self._read_reply(reader)

    async def _read_reply(self, reader: asyncio.StreamReader) -> Any:
        line = await reader.readline()
        if not line:
            raise ConnectionError("redis closed the connection")
        kind, payload = line[:1], line[1:-2].decode(errors="replace")
        if kind == b"+":  # simple string
            return payload
        if kind == b"-":  # error
            raise ConnectionError(f"redis error: {payload}")
        if kind == b":":  # integer
            return int(payload)
        if kind == b"$":  # bulk string
            length = int(payload)
            if length == -1:
                return None
            data = await reader.readexactly(length + 2)
            return data[:-2]
        if kind == b"*":  # array
            count = int(payload)
            if count == -1:
                return None
            return [await self._read_reply(reader) for _ in range(count)]
        raise ConnectionError(f"unexpected redis reply: {line!r}")

    async def get(self, key: str, default: Any = None) -> Any:
        raw = await self._roundtrip("GET", key)
        if raw is None:
            return default
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw

    async def set(self, key: str, value: Any, expire: float | None = None, **_: Any) -> None:
        payload = value if isinstance(value, str) else json.dumps(value)
        if expire:
            await self._roundtrip("SET", key, payload, "EX", int(expire))
        else:
            await self._roundtrip("SET", key, payload)

    async def delete(self, key: str) -> None:
        await self._roundtrip("DEL", key)

    async def close(self) -> None:
        return None


def _build_backend(backend: str) -> Any:
    if backend.startswith(("redis://", "rediss://")):
        return _RedisBackend(backend)
    # Everything else is in-memory ("mem://", "mem://?size=N", "" ...).
    return _MemoryBackend(_parse_mem_size(backend or "mem://"))


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
        self.token = token
        self.key = f"{KEY_PREFIX}:{token}"
        self.ttl = ttl
        self.min_save_interval = min_save_interval
        self.backend = backend
        self._last_save = 0.0
        self._closed = False
        # A distinct backend per instance: nothing is shared unless the caller
        # goes through for_token(). Use hashlib for a stable, non-secret hash.
        token_hash = hashlib.md5(token.encode("utf-8")).hexdigest()
        self.name = name or f"despise-gw-{token_hash}"
        self._cache = _build_backend(backend)

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
