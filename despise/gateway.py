"""Gateway lifecycle: Hello/Identify/READY/dispatch/resume/QoS.

Borrows the ONE shared session from DespiseHttp (same impersonate/proxy).
Single heartbeat task. No zlib-stream (json encoding, v1 scope).

Lifecycle contract:
- ``connect()`` performs the handshake, hands the socket to a *tracked*
  reader task, and returns once READY (or the handshake times out).
- The reader, the heartbeat, and the rotate task are the only three
  background tasks, all owned by ``_stop_tasks``. Nothing spawns an
  untracked task, so teardown can never leak a reader across a reconnect.
- ``_reconnect`` never runs on top of a cancelled task: it stops the
  other tasks first, then re-enters ``connect()``. Closing the socket is
  what decides resume-vs-fresh (see ``FATAL_CLOSE_CODES``).
- ``close()`` is final. After it, ``_closed`` is set and ``_reconnect``
  becomes a no-op, so a dying socket cannot resurrect the client.

discord.food notes honored:
- Identify properties include gateway-only fields (installation_id is
  omitted; is_fast_connect False) + ephemeral ids.
- User presence: first Identify sends no presence (unknown/empty);
  rich presence (op 3) only after READY, throttled client-side.
- resume via resume_gateway_url; PRIORITIZED_READY omitted
  (would require DEDUPE_USER_OBJECTS).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
import uuid
import zlib
from collections.abc import Callable
from typing import Any

from .exceptions import (
    AuthenticationFailed,
    GatewayClosed,
    GatewayTimeout,
    InvalidIntents,
    NotConnected,
    TooManySubscriptions,
)
from .headers import super_properties_dict
from .http import DespiseHttp
from .models import User

log = logging.getLogger("despise.gateway")

GATEWAY_VERSION = 9
CAPABILITIES = 1734653
QOS_VERSION = 29

# opcodes
DISPATCH = 0
HEARTBEAT = 1
IDENTIFY = 2
PRESENCE_UPDATE = 3
RESUME = 6
RECONNECT = 7
REQUEST_GUILD_MEMBERS = 8
INVALID_SESSION = 9
HELLO = 10
HEARTBEAT_ACK = 11
# op 12 = lazy member-list sync chunks (GUILD_MEMBERS_UPDATE / SYNC_MEMBERS
# / LAZY_LOAD_FAILED), streamed in reply to an op 8 with limit=0.
GUILD_MEMBER_LIST_SYNC = 12
GUILD_SUBSCRIPTIONS = 14
QOS = 40
UPDATE_TIME_SPENT = 41

# close codes that must NOT resume: fatal, or the session is unusable
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
# bad seq / session timed out -> re-identify instead of resuming
FRESH_CLOSE_CODES = {4007, 4009}
# 4008 = identify throttled. Discord enforces a per-token cooldown, so a
# fast retry loop only makes the penalty worse.
IDENTIFY_THROTTLED_CLOSE_CODE = 4008

HEARTBEAT_SESSION_ROTATE_S = 30 * 60
HELLO_TIMEOUT_S = 15.0
READY_TIMEOUT_S = 30.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
MAX_RECONNECT_ATTEMPTS = 10
PRESENCE_MIN_INTERVAL_S = 5.0

# "json" = plain text frames. "zlib-stream" compresses each message payload
# with zlib and sends it as a binary frame: much less bandwidth on
# guild-heavy accounts, at the cost of doing the codec ourselves.
GATEWAY_ENCODINGS = ("json", "zlib-stream")


class Gateway:
    """One gateway connection. Owned by Client, borrows DespiseHttp."""

    def __init__(
        self,
        http: DespiseHttp,
        token: str,
        *,
        intents: int = 0,
        listeners: dict | None = None,
        max_backoff: float = BACKOFF_MAX_S,
        encoding: str = "json",
        session_cache: Any = None,
    ) -> None:
        if encoding not in GATEWAY_ENCODINGS:
            raise ValueError(
                f"unknown gateway encoding {encoding!r}, "
                f"expected one of {GATEWAY_ENCODINGS}"
            )
        self.http = http
        self.token = token
        self.intents = intents
        self.encoding = encoding
        self.session_cache = session_cache
        self.listeners: dict[str, list] = dict(listeners or {})
        self.error_listeners: list = []
        self.seq: int | None = None
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.user: User | None = None
        self.max_backoff = max_backoff
        # READY reports max_concurrency (how many guild subscriptions the
        # account may hold). Exceeding it is close code 4013.
        self.max_concurrency: int | None = None
        self.subscribed_guilds: set[str] = set()
        self._ws: Any = None
        self._ctx: Any = None
        self._beat_task: asyncio.Task | None = None
        self._rotate_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._acked = asyncio.Event()
        self._acked.set()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        self._waiters: dict[str, list] = {}
        self._attempts = 0
        self._reconnect_lock = asyncio.Lock()
        self._close_error: BaseException | None = None
        self._presence_lock = asyncio.Lock()
        self._last_presence_at = 0.0
        # Last presence we announced, replayed on a re-Identify. Discord
        # expects it there: a resumed-from-nothing client that silently drops
        # to "online, no activity" looks nothing like a real one.
        self._presence: dict | None = None

    # -- events ---------------------------------------------------------
    def on(self, event: str, coro) -> None:
        self.listeners.setdefault(event, []).append(coro)

    def on_error(self, coro) -> None:
        """Register a ``async (event_name, exception)`` handler."""
        self.error_listeners.append(coro)

    async def _dispatch_error(self, event: str, exc: BaseException) -> None:
        log.warning("listener for %s raised %r", event, exc)
        for handler in self.error_listeners:
            try:
                res = handler(event, exc)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("gateway on_error handler failed")

    async def _emit(self, event: str, data) -> None:
        """Run listeners isolated: one raising callback must not kill the loop."""
        for listener in list(self.listeners.get(event, ())):
            try:
                res = listener(data)
                if inspect.isawaitable(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._dispatch_error(event, exc)

    async def _settle_waiters(self, event: str, data) -> None:
        for fut, check in list(self._waiters.get(event, ())):
            if fut.done():
                continue
            try:
                matched = check is None or check(data)
                if inspect.isawaitable(matched):
                    matched = await matched
            except Exception as exc:
                fut.set_exception(exc)
                continue
            if matched:
                fut.set_result(data)

    async def wait_for(
        self,
        event: str,
        *,
        check: Callable[[Any], Any] | None = None,
        timeout: float | None = None,
    ):
        """Await the next ``event`` whose payload satisfies ``check``."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        entry = (fut, check)
        self._waiters.setdefault(event, []).append(entry)
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        finally:
            pending = self._waiters.get(event)
            if pending and entry in pending:
                pending.remove(entry)

    # -- payloads -------------------------------------------------------
    def _identify(self) -> dict:
        payload: dict = {
            "token": self.token,
            "properties": super_properties_dict(
                self.http.profile, self.http.eph, for_gateway=True
            ),
            "compress": False,
            "large_threshold": 250,
            "intents": self.intents,
            "capabilities": CAPABILITIES,
            "client_state": {"guild_versions": {}, "api_code_version": 0},
        }
        # The *first* identify legitimately sends no presence (Discord is
        # still deciding what the client is). A re-identify -- after
        # INVALID_SESSION:false, a fresh close code, or a 4008 -- must carry
        # the presence we last announced, or the account silently resets to
        # "online, no activity".
        if self._presence is not None:
            payload["presence"] = self._presence
        return {"op": IDENTIFY, "d": payload}

    def _resume(self) -> dict:
        return {
            "op": RESUME,
            "d": {
                "token": self.token,
                "session_id": self.session_id,
                "seq": self.seq,
            },
        }

    def _qos(self) -> dict:
        return {
            "op": QOS,
            "d": {
                "version": QOS_VERSION,
                "active": True,
                "reasons": {"foregrounded": True, "rtc_connected": False},
            },
        }

    def _time_spent(self) -> dict:
        return {"op": UPDATE_TIME_SPENT, "d": {"time_spent": int(time.time() * 1000)}}

    # -- connection -----------------------------------------------------
    async def _ws_url(self) -> str:
        if self.resume_url:
            url = self.resume_url
        else:
            data = await self.http.request("GET", "/gateway", auth=True)
            if not isinstance(data, dict):
                raise GatewayClosed(f"unexpected /gateway payload: {data!r}")
            url = data["url"]
        return f"{url}?v={GATEWAY_VERSION}&encoding={self.encoding}"

    # -- framing ---------------------------------------------------------
    # "json" is plain text frames. "zlib-stream" compresses each message
    # payload with zlib and sends it as a binary frame, which is a large
    # bandwidth win on guild-heavy accounts but means we do the codec
    # ourselves instead of letting the websocket lib.
    def _encode_payload(self, payload: dict) -> bytes:
        return zlib.compress(json.dumps(payload, separators=(",", ":")).encode())

    async def _send_frame(self, ws, payload: dict) -> None:
        if self.encoding == "zlib-stream":
            await ws.send_bytes(self._encode_payload(payload))
        else:
            await ws.send_json(payload)

    async def _recv_frame(self, ws):
        if self.encoding == "zlib-stream":
            raw, _frame_type = await ws.recv()
            if isinstance(raw, str):
                raw = raw.encode()
            return json.loads(zlib.decompress(raw))
        return await ws.recv_json()

    async def _restore_session(self) -> None:
        """Adopt a cached session so the next Identify can be a Resume."""
        if self.session_cache is None or self.session_id is not None:
            return
        state = await self.session_cache.load()
        if state is None:
            return
        log.info(
            "restored cached gateway session %s (seq=%s) from %s",
            state.session_id,
            state.seq,
            type(self.session_cache).__name__,
        )
        self.session_id = state.session_id
        self.seq = state.seq
        if state.resume_url:
            self.resume_url = state.resume_url

    async def _persist_session(self, *, force: bool = False) -> None:
        if self.session_cache is None or self.session_id is None:
            return
        await self.session_cache.save(
            self.session_id,
            self.seq,
            self.resume_url,
            force=force,
        )

    async def _forget_session(self) -> None:
        """Drop the cached session; the next connect must re-identify."""
        if self.session_cache is None:
            return
        await self.session_cache.clear()

    async def connect(self) -> None:
        """Handshake + Identify/Resume, then background reads. Returns on READY.

        Re-entrant: ``_reconnect`` calls this after stopping the old tasks.
        """
        async with self._connect_lock:
            if self._closed.is_set():
                return
            await self._connect()

    async def _connect(self) -> None:
        await self._restore_session()
        can_resume = self.session_id is not None and self.seq is not None
        url = await self._ws_url()
        if self._closed.is_set():
            return

        ctx = self.http.session.ws_connect(url)
        ws = await ctx.__aenter__()
        self._ws = ws
        self._ctx = ctx
        self._acked.set()
        self._ready.clear()
        if self._closed.is_set():
            # close() landed while we were opening the socket
            await self._teardown()
            return

        try:
            hello = await self._recv_json(ws, HELLO_TIMEOUT_S)
            if hello.get("op") != HELLO:
                raise GatewayClosed(f"expected HELLO, got op={hello.get('op')!r}")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0

            # Immediate heartbeat (required before any other frame), then the
            # session payload, then the client-state chatter real clients send.
            await self._send_frame(ws, {"op": HEARTBEAT, "d": self.seq})
            await self._send_frame(ws, self._resume() if can_resume else self._identify())
            await self._send_frame(ws, self._qos())
            await self._send_frame(ws, self._time_spent())
        except (GatewayTimeout, GatewayClosed):
            await self._teardown()
            raise
        except asyncio.CancelledError:
            await self._teardown()
            raise
        except Exception as exc:
            # never leave a half-open socket behind
            await self._teardown()
            raise GatewayClosed(f"gateway handshake failed: {exc!r}") from None

        self._beat_task = asyncio.create_task(
            self._beat_loop(interval), name="despise-heartbeat"
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="despise-reader"
        )
        try:
            # Race READY against close(): waiting only on _ready would let a
            # concurrent close() return while this handshake still owned the
            # socket, orphaning it and these two tasks.
            await self._wait_ready_or_closed(READY_TIMEOUT_S)
        except GatewayTimeout:
            await self._teardown()
            if self._closed.is_set():
                return  # close() won the race; not a handshake failure
            raise
        except Exception:
            await self._teardown()
            raise
        if self._closed.is_set():
            await self._teardown()
            return
        self._attempts = 0
        self._start_rotate_task()

    async def _wait_ready_or_closed(self, timeout: float) -> None:
        ready = asyncio.ensure_future(self._ready.wait())
        closed = asyncio.ensure_future(self._closed.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready, closed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            ready.cancel()
            closed.cancel()
        if ready in done:
            return
        raise GatewayTimeout(f"READY not received within {timeout}s")

    async def _recv_json(self, ws, timeout: float):
        try:
            return await asyncio.wait_for(self._recv_frame(ws), timeout)
        except asyncio.TimeoutError:
            raise GatewayTimeout(f"no frame within {timeout}s") from None

    # -- background tasks ------------------------------------------------
    async def _beat_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._acked.is_set():
                    log.warning("heartbeat unacked; treating socket as a zombie")
                    await self._reconnect(resume=True, cause="heartbeat unacked")
                    return
                self._acked.clear()
                await self._send_frame(self._ws, {"op": HEARTBEAT, "d": self.seq})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("heartbeat failed: %r", exc)
            await self._reconnect(resume=True, cause=f"heartbeat error: {exc!r}")

    async def _rotate_loop(self) -> None:
        """Every 30min: rotate heartbeat session id + op40/op41."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SESSION_ROTATE_S)
                self.http.eph.client_heartbeat_session_id = uuid.uuid4().hex
                self.http.refresh_super_properties()
                await self._send_frame(self._ws, self._qos())
                await self._send_frame(self._ws, self._time_spent())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("rotate tick skipped: %r", exc)

    def _start_rotate_task(self) -> None:
        if self._rotate_task and not self._rotate_task.done():
            return
        self._rotate_task = asyncio.create_task(
            self._rotate_loop(), name="despise-rotate"
        )

    async def _read_loop(self) -> None:
        """The one tracked reader. Ends only by reconnecting or dying."""
        try:
            while True:
                ws = self._ws
                if ws is None:
                    return
                msg = await self._recv_frame(ws)
                op = msg.get("op")

                if op == DISPATCH:
                    self.seq = msg.get("s", self.seq)
                    await self._persist_session()  # throttled; READY forces
                    await self._handle_dispatch(msg.get("t", ""), msg.get("d"))
                elif op == HEARTBEAT_ACK:
                    self._acked.set()
                elif op == RECONNECT:
                    await self._reconnect(resume=True, cause="server RECONNECT")
                    return
                elif op == INVALID_SESSION:
                    resumable = bool(msg.get("d", False))
                    await self._reconnect(
                        resume=resumable,
                        cause=f"INVALID_SESSION resumable={resumable}",
                    )
                    return
                elif op == GUILD_MEMBER_LIST_SYNC:
                    # Lazy member-list chunks: GUILD_MEMBERS_UPDATE,
                    # SYNC_MEMBERS, or LAZY_LOAD_FAILED. Surfaced under both
                    # the specific name and a generic one so wait_for() is
                    # convenient either way.
                    data = msg.get("d") or {}
                    kind = msg.get("t") or "GUILD_MEMBERS_UPDATE"
                    await self._emit(kind, data)
                    await self._emit("GUILD_MEMBERS_UPDATE", data)
                    await self._settle_waiters(kind, data)
                    await self._settle_waiters("GUILD_MEMBERS_UPDATE", data)
                elif op == HEARTBEAT:
                    await self._send_frame(ws, {"op": HEARTBEAT, "d": self.seq})
                elif op in (HELLO, 0):
                    continue
                else:
                    log.debug("unhandled gateway op %s", op)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._reconnect(resume=True, cause=f"read error: {exc!r}")

    async def _handle_dispatch(self, kind: str, data: dict) -> None:
        if kind == "READY":
            self.session_id = data["session_id"]
            self.resume_url = data["resume_gateway_url"]
            self.user = User(data["user"])
            self.max_concurrency = data.get("max_concurrency")
            self.subscribed_guilds.clear()
            self._ready.set()
            await self._persist_session(force=True)
        elif kind == "RESUMED":
            self._attempts = 0
            self._ready.set()
            await self._persist_session(force=True)
        elif kind == "GUILD_DELETE":
            if not data.get("guild_id"):
                self.subscribed_guilds.clear()
            else:
                self.subscribed_guilds.discard(str(data["guild_id"]))
        await self._emit(kind, data)
        await self._settle_waiters(kind, data)

    # -- reconnect ------------------------------------------------------
    def _close_code(self) -> tuple[int | None, str | None]:
        ws = self._ws
        if ws is None:
            return None, None
        code = getattr(ws, "close_code", None)
        reason = getattr(ws, "close_reason", None)
        return (code if isinstance(code, int) else None, reason)

    def _classify(
        self, code: int | None, reason: str | None, resume_hint: bool
    ) -> tuple[bool, float, BaseException | None]:
        """Close code -> (resume?, backoff seconds, fatal error or None)."""
        if code in FATAL_CLOSE_CODES:
            detail = reason or "fatal gateway close"
            if code == 4004:
                err: BaseException = AuthenticationFailed(detail, code=code, reason=reason)
            elif code == 4014:
                err = InvalidIntents(detail, code=code, reason=reason)
            else:
                err = GatewayClosed(detail, code=code, reason=reason)
            return False, 0.0, err

        if code in FRESH_CLOSE_CODES:
            resume_hint = False

        self._attempts += 1
        if code == IDENTIFY_THROTTLED_CLOSE_CODE:
            # Never hammer Identify: back off harder than a normal blip.
            delay = max(BACKOFF_BASE_S, min(self.max_backoff, 2**self._attempts))
            log.warning("identify throttled by gateway; waiting %.1fs", delay)
            return False, delay, None

        delay = min(self.max_backoff, BACKOFF_BASE_S * 2 ** (self._attempts - 1))
        return resume_hint, delay * random.uniform(1.0, 1.5), None

    async def _reconnect(self, *, resume: bool, cause: str = "") -> None:
        """Tear down and re-enter ``connect``. Safe to call from any task.

        The lock means that if the heartbeat and the reader both notice the
        socket is gone, only one reconnect happens. A handshake that itself
        fails is retried (with growing backoff) rather than left to kill the
        caller, which would leave the client permanently silent.
        """
        if self._closed.is_set():
            return
        async with self._reconnect_lock:
            if self._closed.is_set():
                return
            log.info("gateway reconnecting (%s)", cause or "unspecified")

            code, reason = await self._teardown()
            resume, delay, fatal = self._classify(code, reason, resume)
            if fatal is not None:
                await self._discard_session()
                self._finish(fatal)
                return

            if not resume:
                await self._discard_session()

            for attempt in range(MAX_RECONNECT_ATTEMPTS):
                if self._closed.is_set():
                    return
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    await self.connect()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "reconnect attempt %d/%d failed: %r",
                        attempt + 1,
                        MAX_RECONNECT_ATTEMPTS,
                        exc,
                    )
                    # A failed handshake tells us nothing about the close
                    # reason; keep the session but back off harder.
                    _resume, delay, fatal = self._classify(None, None, resume)
                    if fatal is not None:
                        await self._discard_session()
                        self._finish(fatal)
                        return

            log.error("gateway gave up after %d reconnect attempts", MAX_RECONNECT_ATTEMPTS)
            self._finish(
                GatewayClosed(
                    f"reconnect failed after {MAX_RECONNECT_ATTEMPTS} attempts"
                )
            )

    def _finish(self, error: BaseException) -> None:
        """Terminal state: record the cause and stop reconnecting forever."""
        log.error("gateway closed permanently: %s", error)
        self._close_error = error
        self._closed.set()

    async def _discard_session(self) -> None:
        """Stop resuming: the session is gone or unusable from now on."""
        self.session_id = None
        self.seq = None
        self.resume_url = None
        self.subscribed_guilds.clear()
        # The cached copy must go too, or the next process resurrects a
        # session Discord has already dropped.
        await self._forget_session()

    async def _stop_tasks(self) -> None:
        """Cancel background tasks -- but never the one calling us."""
        current = asyncio.current_task()
        tracked = (self._beat_task, self._rotate_task, self._reader_task)
        doomed = [t for t in tracked if t is not None and t is not current]
        for task in doomed:
            if not task.done():
                task.cancel()
        pending = [t for t in doomed if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._beat_task = self._rotate_task = self._reader_task = None

    async def _teardown(self) -> tuple[int | None, str | None]:
        """Stop tasks, close the socket, and report the close code."""
        code, reason = self._close_code()
        await self._stop_tasks()
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                log.debug("socket close raised", exc_info=True)
        self._ws = None
        self._ctx = None
        self._ready.clear()
        self._acked.set()
        return code, reason

    # -- public socket ops ----------------------------------------------
    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnected("gateway is not connected")
        await self._send_frame(ws, payload)

    async def send_presence(
        self, activities: list, *, status: str = "online", force: bool = False
    ) -> None:
        """op3 rich presence, throttled to one update per 5s per connection."""
        async with self._presence_lock:
            now = time.monotonic()
            wait = PRESENCE_MIN_INTERVAL_S - (now - self._last_presence_at)
            if wait > 0 and not force:
                await asyncio.sleep(wait)
            self._last_presence_at = time.monotonic()
            # Remembered so a re-Identify can replay it.
            self._presence = {
                "status": status,
                "activities": activities,
                "since": 0,
                "afk": False,
            }
            await self._send({"op": PRESENCE_UPDATE, "d": dict(self._presence)})

    async def clear_presence(self, *, status: str = "online") -> None:
        await self.send_presence([], status=status, force=True)

    async def subscribe_guild(
        self, guild_id: str, *, channels: dict | None = None
    ) -> None:
        """op14 guild channel-subscribe.

        Refuses to exceed READY's ``max_concurrency``: the server answers an
        over-subscribed Identify/reconnect with close 4013, which costs a
        reconnect. Catching it here is strictly cheaper.
        """
        guild_id = str(guild_id)
        cap = self.max_concurrency
        if (
            cap is not None
            and guild_id not in self.subscribed_guilds
            and len(self.subscribed_guilds) >= cap
        ):
            raise TooManySubscriptions(
                f"account may hold {cap} guild subscriptions; "
                f"unsubscribe before adding {guild_id}",
                limit=cap,
                guild_id=guild_id,
            )
        await self._send(
            {
                "op": GUILD_SUBSCRIPTIONS,
                "d": {
                    "guild_id": guild_id,
                    "typing": True,
                    "threads": True,
                    "activities": True,
                    "channels": channels or {},
                },
            }
        )
        self.subscribed_guilds.add(guild_id)

    async def unsubscribe_guild(self, guild_id: str) -> None:
        """Release a subscription slot (op 14 with everything switched off)."""
        guild_id = str(guild_id)
        await self._send(
            {
                "op": GUILD_SUBSCRIPTIONS,
                "d": {
                    "guild_id": guild_id,
                    "typing": False,
                    "threads": False,
                    "activities": False,
                    "channels": {},
                },
            }
        )
        self.subscribed_guilds.discard(guild_id)

    async def request_members(
        self,
        guild_id: str,
        *,
        query: str = "",
        limit: int = 0,
        user_ids: list[str] | None = None,
        nonce: str | None = None,
    ) -> str:
        """op8 member request. Returns the nonce to match the reply on.

        ``limit=0`` opts into lazy member-list sync: the server then streams
        op 12 chunks instead of a single reply.
        """
        nonce = nonce or uuid.uuid4().hex
        await self._send(
            {
                "op": REQUEST_GUILD_MEMBERS,
                "d": {
                    "guild_id": guild_id,
                    "query": query,
                    "limit": limit,
                    "presences": False,
                    "user_ids": user_ids or [],
                    "nonce": nonce,
                },
            }
        )
        return nonce

    async def lazy_load_members(self, guild_id: str, *, nonce: str | None = None) -> str:
        """Opt into lazy member-list sync for ``guild_id``.

        Returns the nonce; chunks arrive as ``GUILD_MEMBERS_UPDATE`` events
        until ``chunk_index == chunk_count``.
        """
        return await self.request_members(guild_id, limit=0, nonce=nonce)

    # -- shutdown --------------------------------------------------------
    async def close(self) -> None:
        """Final shutdown. Idempotent; no further reconnect may happen."""
        self._closed.set()
        self._ready.clear()
        await self._stop_tasks()
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                log.debug("socket close raised", exc_info=True)
        self._ws = None
        self._ctx = None
        # A deliberate shutdown ends the session for good; leaving it cached
        # would make the next start resume a session Discord has retired.
        await self._discard_session()

    async def wait_closed(self) -> None:
        """Block until the connection ends for good; raise if it was fatal."""
        await self._closed.wait()
        if self._close_error is not None:
            raise self._close_error

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def ready_event(self) -> asyncio.Event:
        return self._ready
