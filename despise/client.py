"""Client: owns profile (restart-frozen) + single shared DespiseHttp + Gateway.

Boot (restart-only): resolve all versions over the shared session ->
rebuild profile with them, set token, connect gateway. Nothing refreshes
mid-run except ephemeral ids.

Lifecycle:
    await bot.start()   # boot + gateway handshake, returns on READY
    await bot.run()     # start() + block until disconnected or closed
    async with Client(...) as bot: ...

``run()`` blocks because the gateway's real work happens in background
tasks; an await that returned would let the caller -- and usually
``asyncio.run`` -- tear the socket down with it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any

from .cache import EntityCache
from .exceptions import AuthenticationHTTPError, DespiseError, Forbidden, NotFound
from .gateway import GATEWAY_ENCODINGS, Gateway
from .headers import EphemeralIds, build_profile
from .http import DespiseHttp
from .models import Channel, Model, User
from .rest import Rest
from .session_cache import SessionCache
from .tasks import Task
from .versions import coerce_resolver

log = logging.getLogger("despise.client")

# Default retry parameters for boot()
BOOT_MAX_RETRIES = 3
BOOT_INITIAL_DELAY = 1.0
BOOT_MAX_DELAY = 10.0


class Client:
    def __init__(
        self,
        token: str,
        *,
        profile: str = "web",
        locale: str | None = None,
        timezone: str | None = None,
        hardware_concurrency: int | None = None,
        screen_resolution: str = "1920x1080",
        device_memory: int = 8,
        proxy: str | None = None,
        timeout: float = 30.0,
        api_base: str | None = None,
        gateway_encoding: str = "zlib-stream",
        session_cache: bool | str = False,
        version_resolver=None,
        cache_caps: tuple[int, int, int] | None = None,
        command_prefix: str = "!",
    ) -> None:
        self.token = token
        self._preset = profile
        self._locale = locale
        self._timezone = timezone
        self._hardware_concurrency = hardware_concurrency
        self._screen_resolution = screen_resolution
        self._device_memory = device_memory
        self.profile = build_profile(
            profile,
            locale=locale,
            timezone=timezone,
            hardware_concurrency=hardware_concurrency,
            screen_resolution=screen_resolution,
            device_memory=device_memory,
        )
        self.ephemeral = EphemeralIds(client_launch_id=uuid.uuid4().hex)
        http_kwargs: dict[str, Any] = {"proxy": proxy, "timeout": timeout}
        if api_base is not None:
            http_kwargs["api_base"] = api_base
        self.http = DespiseHttp(self.profile, self.ephemeral, **http_kwargs)
        self.rest = Rest(self.http, client=self)
        # A rejected token stops the whole client; see _on_auth_dead.
        self._auth_terminated = False
        self._shutdown_task: asyncio.Task | None = None
        self.http.on_auth_dead(self._on_auth_dead)
        if gateway_encoding not in GATEWAY_ENCODINGS:
            raise ValueError(
                f"unknown gateway_encoding {gateway_encoding!r}, "
                f"expected one of {GATEWAY_ENCODINGS}"
            )
        # `session_cache=True` -> in-memory; pass a URL ("redis://...") for
        # a backend that survives a restart. Built in, no extra install.
        self.session_cache: SessionCache | None = None
        if session_cache is not False and session_cache is not None:
            from .session_cache import for_token

            backend = session_cache if isinstance(session_cache, str) else "mem://"
            self.session_cache = for_token(token, backend=backend)
        self.gateway = Gateway(
            self.http,
            token,
            encoding=gateway_encoding,
            session_cache=self.session_cache,
        )
        self._booted = False
        # EntityCache takes keyword-only caps; splatting the tuple raised
        # ``TypeError`` for every caller that passed one.
        if cache_caps:
            self.cache = EntityCache(
                guild_cap=cache_caps[0],
                channel_cap=cache_caps[1],
                user_cap=cache_caps[2],
            )
        else:
            self.cache = EntityCache()
        self._listeners: dict[str, list] = {}
        self._raw_listeners: dict[str, list] = {}
        self._client_waiters: dict[str, list] = {}
        self._bridged: set[str] = set()
        # Bridge every known dispatch up front. Seeding (READY guilds) and
        # wait_for must work even when the user registers no listeners.
        from .events import RAW_TO_FRIENDLY as _RAW_TO_FRIENDLY

        for _raw in _RAW_TO_FRIENDLY:
            self._ensure_bridge(_raw)
        self.tasks: list[Task] = []
        self.version_resolver = coerce_resolver(version_resolver)
        self._register_cache_listeners()
        self.command_prefix = command_prefix
        from .cmd import ModuleManager

        self.modules = ModuleManager(self)

    # -- events ---------------------------------------------------------
    # Friendly discord.py-self style names on top of raw gateway dispatches.
    # ``bot.on("message", ...)`` and ``@bot.event async def on_message(m)``
    # receive a model. ``bot.on("MESSAGE_CREATE", ...)`` still receives the
    # raw dict, exactly as before. Dispatches with no friendly mapping
    # (``MESSAGE_POLL_VOTE_ADD``, ``CALL_UPDATE``, ...) work too -- by their
    # raw name or any spelling of it -- and deliver the raw payload.
    def on(self, event: str, coro=None):
        from .events import route

        kind, key, raw = route(event)
        if raw is not None:
            self._ensure_bridge(raw)
        bucket = self._raw_listeners if kind == "raw" else self._listeners
        bucket.setdefault(key, []).append(coro)
        return coro

    def _unhook(self, coro) -> None:
        """Remove a listener from every bucket. Used to unload modules."""
        for bucket in (self._listeners, self._raw_listeners):
            for key in list(bucket):
                bucket[key] = [c for c in bucket[key] if c is not coro]
                if not bucket[key]:
                    del bucket[key]

    def _ensure_bridge(self, raw: str) -> None:
        if raw in self._bridged:
            return
        self._bridged.add(raw)

        async def _bridge(payload) -> None:
            await self._handle_raw(raw, payload)

        self.gateway.on(raw, _bridge)

    async def _handle_raw(self, raw: str, payload) -> None:
        from .events import NO_ARG_EVENTS, convert

        if raw == "READY":
            self._seed_from_ready(payload)
        friendly, value = convert(raw, payload)
        if isinstance(value, Model):
            value._bind(self)
        targets: list[tuple] = []
        if friendly is not None and friendly not in NO_ARG_EVENTS:
            targets += [(c, value) for c in self._listeners.get(friendly, ())]
        elif friendly in NO_ARG_EVENTS:
            targets += [(c, None) for c in self._listeners.get(friendly, ())]
        targets += [(c, payload) for c in self._raw_listeners.get(raw, ())]
        for coro, arg in targets:
            try:
                res = coro() if arg is None else coro(arg)
                if inspect.isawaitable(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._dispatch_error(friendly or raw, exc)
        await self._settle_client_waiters(raw, friendly, payload, value)

    def _seed_from_ready(self, payload) -> None:
        """Feed the entity cache from READY's embedded guilds and DMs.

        READY arrives with full guild objects (channels, roles, members),
        not follow-up GUILD_CREATEs, so without this every ``get_guild``
        after connect burns a REST call. Never raises; a malformed READY
        must not break the handshake.
        """
        try:
            if not isinstance(payload, dict):
                return
            from .models import Channel, Guild

            for g in payload.get("guilds", []) or []:
                if isinstance(g, dict) and not g.get("unavailable"):
                    try:
                        self.cache.put_guild(Guild(g))
                    except Exception:
                        continue
            for c in payload.get("private_channels", []) or []:
                if isinstance(c, dict):
                    try:
                        self.cache.put_channel(Channel(c))
                    except Exception:
                        continue
        except Exception:
            log.exception("seeding cache from READY failed")

    async def _settle_client_waiters(self, raw: str, friendly: str | None, payload, value) -> None:
        from .events import NO_ARG_EVENTS

        settled: list[tuple] = []
        if friendly is not None:
            settled += [(w, value if friendly not in NO_ARG_EVENTS else None)
                        for w in self._client_waiters.get(friendly, ())]
        settled += [(w, payload) for w in self._client_waiters.get(raw, ())]
        if friendly is not None:
            settled += [(w, value) for w in self._client_waiters.get(raw.lower(), ())]
        for (fut, check), arg in settled:
            if fut.done():
                continue
            try:
                matched = check is None or check(arg)
                if inspect.isawaitable(matched):
                    matched = await matched
            except Exception as exc:
                fut.set_exception(exc)
                continue
            if matched:
                fut.set_result(arg)

    async def _dispatch_error(self, event: str, exc: BaseException) -> None:
        for handler in list(getattr(self.gateway, "error_listeners", ())):
            try:
                res = handler(event, exc)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("client on_error handler failed")

    def _register_cache_listeners(self) -> None:
        """Keep the entity cache fed from dispatches.

        These run before user listeners are appended, so a handler that calls
        ``get_guild`` during a dispatch sees the fresh object.
        """
        from .models import Channel, Guild, Member, Message

        def as_model(cls, payload):
            return cls(payload) if isinstance(payload, dict) else None

        def on_guild(payload):
            if isinstance(payload, dict) and not payload.get("unavailable"):
                guild = as_model(Guild, payload)
                if guild is not None:
                    self.cache.put_guild(guild)

        def on_guild_delete(payload):
            if isinstance(payload, dict) and payload.get("guild_id"):
                self.cache.drop_guild(str(payload["guild_id"]))
            elif isinstance(payload, dict) and payload.get("id"):
                self.cache.drop_guild(str(payload["id"]))

        def on_channel(payload):
            if isinstance(payload, dict):
                channel = as_model(Channel, payload)
                if channel is not None:
                    self.cache.put_channel(channel)

        def on_channel_delete(payload):
            if isinstance(payload, dict) and payload.get("id"):
                self.cache.drop_channel(str(payload["id"]))

        def on_member(payload):
            if isinstance(payload, dict):
                member = as_model(Member, payload)
                if member is not None:
                    self.cache.put_member(member)

        def on_message(payload):
            if isinstance(payload, dict):
                message = as_model(Message, payload)
                if message is not None:
                    self.cache.put_message_author(message.author)
                    if message.member is not None:
                        self.cache.put_member(message.member)

        self.gateway.on("GUILD_CREATE", on_guild)
        self.gateway.on("GUILD_UPDATE", on_guild)
        self.gateway.on("GUILD_DELETE", on_guild_delete)
        self.gateway.on("CHANNEL_CREATE", on_channel)
        self.gateway.on("CHANNEL_UPDATE", on_channel)
        self.gateway.on("CHANNEL_DELETE", on_channel_delete)
        self.gateway.on("GUILD_MEMBER_ADD", on_member)
        self.gateway.on("GUILD_MEMBER_UPDATE", on_member)
        self.gateway.on("MESSAGE_CREATE", on_message)
        self.gateway.on("MESSAGE_UPDATE", on_message)

    # -- cached lookups -------------------------------------------------
    async def get_guild(self, guild_id: str):
        """Cached guild, else fetched over REST.

        Falls back to the cached copy if the request fails -- a guild we are
        already in is not worth failing an event handler over.
        """
        key = str(guild_id)
        cached = self.cache.get_guild(key)
        if cached is not None:
            return cached
        try:
            guild = await self.rest.get_guild(key)
        except NotFound:
            return None
        except DespiseError as exc:
            log.debug("get_guild(%s) failed: %r", key, exc)
            return None
        self.cache.put_guild(guild)
        return guild

    async def get_channel(self, channel_id: str):
        key = str(channel_id)
        cached = self.cache.get_channel(key)
        if cached is not None:
            return cached._bind(self)
        try:
            data = await self.http.request("GET", f"/channels/{key}")
        except (NotFound, Forbidden):
            return None
        except DespiseError as exc:
            log.debug("get_channel(%s) failed: %r", key, exc)
            return None
        assert isinstance(data, dict)
        channel = Channel(data)
        self.cache.put_channel(channel)
        return channel._bind(self)

    async def get_user(self, user_id: str):
        key = str(user_id)
        cached = self.cache.get_user(key)
        if cached is not None:
            return cached._bind(self)
        try:
            data = await self.http.request("GET", f"/users/{key}")
        except (NotFound, Forbidden):
            return None
        except DespiseError as exc:
            log.debug("get_user(%s) failed: %r", key, exc)
            return None
        assert isinstance(data, dict)
        user = User(data)
        self.cache.put_user(user)
        return user._bind(self)

    def get_member(self, user_id: str, guild_id: str):
        """Cache-only member lookup (no lazy fetch)."""
        return self.cache.get_member(user_id, guild_id)

    def clear_cache(self) -> None:
        self.cache.clear()

    # -- sending --------------------------------------------------------
    async def send(self, target, content: str | None = None, **kwargs):
        """Send a message to a channel, a user, or a message.

        A ``Message`` target replies in its own channel; a ``User`` target
        opens a DM first; anything else is treated as a channel id. The
        ``**kwargs`` are :meth:`~despise.rest.Rest.send_message`'s
        (``files``, ``embeds``, ``components``, ``poll``, ``stickers``, ...).
        """
        from .models import Message

        if isinstance(target, Message):
            kwargs.setdefault("reply_to", target._id)
            target = target.channel_id
        return await self.rest.send_message(
            await self._resolve_channel(target), content, **kwargs
        )

    async def reply(self, message, content: str | None = None, **kwargs):
        """Reply to a ``Message`` (a readability alias for ``send``)."""
        return await self.send(message, content, **kwargs)

    async def _resolve_channel(self, target) -> str:
        if isinstance(target, Channel):
            return target._id
        if isinstance(target, User):
            return (await self.rest.open_dm(target._id))._id
        return str(target)

    async def fetch_message(self, channel_id, message_id):
        return await self.rest.fetch_message(str(channel_id), str(message_id))

    # -- tasks ----------------------------------------------------------
    def add_task(self, task: Task) -> Task:
        """Register a Task so ``close()`` stops it."""
        self.tasks.append(task)
        return task

    def start_task(self, task: Task) -> Task:
        task = self.add_task(task)
        task.start()
        return task

    def cancel_tasks(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        self.tasks.clear()

    def _on_auth_dead(self, exc: BaseException) -> None:
        """A REST 401 means the token is dead -- stop the gateway, don't reconnect.

        There is nothing worth persisting or resuming: cancel every task, tear
        the socket down for good, and let whoever is awaiting ``run()`` see
        ``exc``. Further authenticated requests fail fast without a network hit.
        """
        if self._auth_terminated:
            return
        self._auth_terminated = True
        log.error("token rejected (401): stopping the gateway and all tasks")
        self.cancel_tasks()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, nothing to stop
            self.gateway._finish(exc)
            return
        self._shutdown_task = loop.create_task(self.gateway.terminate(exc))

    def event(self, coro):
        """Decorator form: ``on_message`` registers for ``message``.

        Strips the ``on_`` prefix and normalizes, so ``on_ready``,
        ``on_message``, and ``on_guild_join`` land on ``ready``,
        ``message``, and ``guild_join``.
        """
        self.on(coro.__name__, coro)
        return coro

    def listen(self, name: str | None = None):
        """Decorator form with an explicit name: ``@bot.listen("message")``."""

        def decorator(coro):
            self.on(name or coro.__name__, coro)
            return coro

        return decorator

    def on_error(self, coro, *, event: str | None = None):
        """Register an ``async (event_name, exception)`` handler.

        Global by default; pass ``event="command:ping"`` to only hear
        about one command (or any other event name).
        """
        if event is None:
            self.gateway.on_error(coro)
            return

        async def filtered(name: str, exc: BaseException) -> None:
            if name == event:
                res = coro(name, exc)
                if inspect.isawaitable(res):
                    await res

        self.gateway.on_error(filtered)

    async def dispatch(self, event: str, payload=None) -> None:
        """Emit an event to client listeners, converting the payload.

        Accepts friendly (``"message"``) or raw (``"MESSAGE_CREATE"``)
        names. Raw listeners receive the payload untouched.
        """
        from .events import route

        _kind, key, raw = route(event)
        if raw is not None:
            # One path: gateway._emit reaches the cache listeners and the
            # bridge, which fans out to friendly/raw client listeners.
            self._ensure_bridge(raw)
            await self.gateway._emit(raw, payload)
            return
        friendly = key
        await self.gateway._emit(event, payload)
        for coro in list(self._listeners.get(friendly, ())):
            try:
                res = coro() if payload is None else coro(payload)
                if inspect.isawaitable(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._dispatch_error(friendly, exc)
        await self._settle_client_waiters(friendly, friendly, payload, payload)

    async def wait_for(
        self,
        event: str,
        *,
        check: Callable[[Any], Any] | None = None,
        timeout: float | None = None,
    ):
        """Wait for the next matching event. Friendly or raw names work.

        The ``check`` sees what a listener sees: a model for friendly
        names (``m.author.id``), the raw dict for raw names.
        """
        from .events import route

        _kind, key, raw = route(event)
        if raw is not None:
            self._ensure_bridge(raw)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._client_waiters.setdefault(key, []).append((fut, check))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        finally:
            bucket = self._client_waiters.get(key, [])
            self._client_waiters[key] = [(f, c) for f, c in bucket if f is not fut]
            if not self._client_waiters[key]:
                self._client_waiters.pop(key, None)

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self.gateway.ready_event.wait()
            return
        await asyncio.wait_for(self.gateway.ready_event.wait(), timeout)

    @property
    def user(self) -> User | None:
        return self.gateway.user

    @property
    def is_ready(self) -> bool:
        return self.gateway.is_ready

    @property
    def closed(self) -> bool:
        return self.gateway.closed

    # -- presence -------------------------------------------------------
    async def set_presence(
        self, activities: list[dict], *, status: str = "online", force: bool = False
    ) -> None:
        await self.gateway.send_presence(activities, status=status, force=force)

    async def clear_presence(self, *, status: str = "online") -> None:
        await self.gateway.clear_presence(status=status)

    # -- lifecycle ------------------------------------------------------
    async def boot(self) -> None:
        """Restart-only refresh: resolve versions, rebuild profile, set token."""
        if self._booted:
            return

        # Retry logic for version resolution with exponential backoff
        for attempt in range(BOOT_MAX_RETRIES + 1):
            try:
                versions = await self.version_resolver.resolve(self.http.session)
                self.profile = build_profile(
                    self._preset,
                    versions,
                    locale=self._locale,
                    timezone=self._timezone,
                    hardware_concurrency=self._hardware_concurrency,
                    screen_resolution=self._screen_resolution,
                    device_memory=self._device_memory,
                )
                # Rebuild session headers around the new profile (same session!).
                self.http.set_profile(self.profile)
                self.http.set_token(self.token)
                self.gateway.http = self.http
                self._booted = True
                return
            except AuthenticationHTTPError:
                # A rejected token is not worth retrying; fail immediately.
                raise
            except (DespiseError, Forbidden, NotFound) as e:
                if attempt == BOOT_MAX_RETRIES:
                    raise
                delay = min(BOOT_INITIAL_DELAY * (2 ** attempt), BOOT_MAX_DELAY)
                if attempt:
                    log.info(
                        "boot() attempt %d/%d failed: %r, retrying in %.1fs",
                        attempt + 1,
                        BOOT_MAX_RETRIES,
                        e,
                        delay,
                    )
                await asyncio.sleep(delay)
            except Exception as e:
                # Network errors or other exceptions - also retry
                if attempt == BOOT_MAX_RETRIES:
                    raise
                delay = min(BOOT_INITIAL_DELAY * (2 ** attempt), BOOT_MAX_DELAY)
                if attempt:
                    log.info(
                        "boot() unexpected error: %r, retrying in %.1fs",
                        e,
                        delay,
                    )
                await asyncio.sleep(delay)

    async def start(self) -> Client:
        """Boot + handshake. Returns once READY; keeps running in background."""
        await self.boot()
        await self.gateway.connect()
        return self

    async def run(self) -> None:
        """start() then block until the connection ends or close() is called."""
        await self.start()
        await self.gateway.wait_closed()

    async def close(self) -> None:
        self.cancel_tasks()
        await self.gateway.close()
        await self.http.close()
        if self.session_cache is not None:
            from .session_cache import forget

            await self.session_cache.close()
            forget(self.session_cache)
            self.session_cache = None

    async def __aenter__(self) -> Client:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
