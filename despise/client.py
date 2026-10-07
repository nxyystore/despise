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
import logging
import random
import time
import uuid
from collections.abc import Callable
from typing import Any

from .cache import EntityCache
from .exceptions import DespiseError, Forbidden, NotFound
from .gateway import GATEWAY_ENCODINGS, Gateway
from .headers import EphemeralIds, build_profile
from .http import DespiseHttp
from .models import Channel, User
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
        intents: int = 0,
        locale: str | None = None,
        timezone: str | None = None,
        hardware_concurrency: int | None = None,
        screen_resolution: str = "1920x1080",
        device_memory: int = 8,
        proxy: str | None = None,
        timeout: float = 30.0,
        api_base: str | None = None,
        gateway_encoding: str = "json",
        session_cache: bool | str = False,
        version_resolver=None,
        cache_caps: tuple[int, int, int] | None = None,
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
        self.rest = Rest(self.http)
        if gateway_encoding not in GATEWAY_ENCODINGS:
            raise ValueError(
                f"unknown gateway_encoding {gateway_encoding!r}, "
                f"expected one of {GATEWAY_ENCODINGS}"
            )
        # `session_cache=True` -> in-memory; pass a URL ("redis://...") for
        # a backend that survives a restart. Requires cashews.
        self.session_cache: SessionCache | None = None
        if session_cache is not False and session_cache is not None:
            from .session_cache import for_token

            backend = session_cache if isinstance(session_cache, str) else "mem://"
            self.session_cache = for_token(token, backend=backend)
        self.gateway = Gateway(
            self.http,
            token,
            intents=intents,
            encoding=gateway_encoding,
            session_cache=self.session_cache,
        )
        self._booted = False
        self.cache = EntityCache(*cache_caps) if cache_caps else EntityCache()
        self.tasks: list[Task] = []
        self.version_resolver = coerce_resolver(version_resolver)
        self._register_cache_listeners()

    # -- events ---------------------------------------------------------
    def on(self, event: str, coro) -> None:
        self.gateway.on(event, coro)

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

        self.on("GUILD_CREATE", on_guild)
        self.on("GUILD_UPDATE", on_guild)
        self.on("GUILD_DELETE", on_guild_delete)
        self.on("CHANNEL_CREATE", on_channel)
        self.on("CHANNEL_UPDATE", on_channel)
        self.on("CHANNEL_DELETE", on_channel_delete)
        self.on("GUILD_MEMBER_ADD", on_member)
        self.on("GUILD_MEMBER_UPDATE", on_member)
        self.on("MESSAGE_CREATE", on_message)
        self.on("MESSAGE_UPDATE", on_message)

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
            return cached
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
        return channel

    async def get_user(self, user_id: str):
        key = str(user_id)
        cached = self.cache.get_user(key)
        if cached is not None:
            return cached
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
        return user

    def get_member(self, user_id: str, guild_id: str):
        """Cache-only member lookup (no lazy fetch)."""
        return self.cache.get_member(user_id, guild_id)

    def clear_cache(self) -> None:
        self.cache.clear()

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

    def event(self, coro):
        """Decorator form: registers ``coro`` under its own name."""
        self.gateway.on(coro.__name__, coro)
        return coro

    def on_error(self, coro) -> None:
        self.gateway.on_error(coro)

    async def dispatch(self, event: str, *args) -> None:
        await self.gateway._emit(event, *args)

    async def wait_for(
        self,
        event: str,
        *,
        check: Callable[[Any], Any] | None = None,
        timeout: float | None = None,
    ):
        return await self.gateway.wait_for(event, check=check, timeout=timeout)

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
