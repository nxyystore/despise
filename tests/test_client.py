"""Client lifecycle tests: start/run/close, event wiring, context manager."""

import asyncio
import inspect

import pytest

from despise.client import Client
from despise.exceptions import AuthenticationFailed


class FakeGateway:
    """Stands in for Gateway: records lifecycle calls, never opens a socket."""

    def __init__(self):
        self.http = None
        self.listeners = {}
        self.error_listeners = []
        self.started = 0
        self.closed = False
        self._fatal = None
        self.ready_event = asyncio.Event()
        self.closed_event = asyncio.Event()
        self._emit_calls = []

    async def connect(self):
        self.started += 1
        self.ready_event.set()

    async def close(self):
        self.closed = True
        self.closed_event.set()

    def fail(self, exc):
        """Simulate a fatal close code: wait_closed() raises, like the real one."""
        self._fatal = exc
        self.closed_event.set()

    async def _emit(self, event, *args):
        # Faithful: the real Gateway runs listeners here.
        self._emit_calls.append((event, args))
        for listener in list(self.listeners.get(event, ())):
            result = listener(*args)
            if inspect.isawaitable(result):
                await result

    def on(self, event, coro):
        self.listeners.setdefault(event, []).append(coro)

    def on_error(self, coro):
        self.error_listeners.append(coro)

    async def wait_closed(self):
        await self.closed_event.wait()
        if self._fatal:
            raise self._fatal

    @property
    def is_ready(self):
        return self.ready_event.is_set()

    @property
    def user(self):
        return self.user_obj


def patched(monkeypatch, **kwargs):
    """Build a Client whose gateway is the fake and whose boot is a no-op."""
    c = Client("tok", **kwargs)
    real_gw = c.gateway
    fake = FakeGateway()
    # Client.__init__ already registered its cache listeners on the real
    # gateway; carry them over so dispatch() still reaches them.
    fake.listeners = real_gw.listeners
    fake.error_listeners = real_gw.error_listeners
    c.gateway = fake  # type: ignore[assignment]
    c.gateway.http = c.http
    _patch_boot(monkeypatch)
    return c, fake


async def _no_network(_session):
    from despise.versions import ResolvedVersions

    return ResolvedVersions()


def _patch_boot(monkeypatch):
    """Skip version resolution so tests never touch the network."""
    from despise.versions import VersionResolver

    monkeypatch.setattr(
        VersionResolver, "resolve", lambda self, session: _no_network(session)
    )


def run(coro):
    return asyncio.run(coro)


# -- lifecycle --------------------------------------------------------------
def test_start_returns_after_ready(monkeypatch):
    c, gw = patched(monkeypatch)
    run(c.http.close())


def test_start_boots_then_connects(monkeypatch):
    c, gw = patched(monkeypatch)
    seen = {}

    async def fake_boot():
        seen["booted"] = True

    c.boot = fake_boot  # type: ignore[assignment]

    async def main():
        result = await c.start()
        assert result is c
        await c.close()

    run(main())
    assert seen["booted"] is True
    assert gw.started == 1


def test_run_blocks_until_gateway_closes(monkeypatch):
    """Regression: run() used to return immediately, killing the socket."""
    c, gw = patched(monkeypatch)
    order = []

    async def main():
        run_task = asyncio.create_task(c.run())
        await asyncio.sleep(0.05)
        order.append("still-connected" if not run_task.done() else "returned-early")
        await c.close()
        await asyncio.wait_for(run_task, 1.0)
        order.append("run-returned")

    run(main())
    assert order == ["still-connected", "run-returned"]


def test_run_surfaces_fatal_close_error(monkeypatch):
    c, gw = patched(monkeypatch)
    gw.fail(AuthenticationFailed("bad token", code=4004))

    with pytest.raises(AuthenticationFailed):
        run(c.run())


def test_close_closes_both_halves(monkeypatch):
    c, gw = patched(monkeypatch)
    closed = []

    class FakeHttp:
        def __init__(self):
            self.http = None

        async def close(self):
            closed.append("http")

    c.http = FakeHttp()  # type: ignore[assignment]
    run(c.close())
    assert gw.closed is True
    assert closed == ["http"]


def test_async_context_manager(monkeypatch):
    c, gw = patched(monkeypatch)

    async def main():
        async with c as bot:
            assert bot is c
            assert gw.started == 1
        assert gw.closed is True

    run(main())


# -- wiring -----------------------------------------------------------------
def test_event_decorator_registers_under_its_name(monkeypatch):
    c, _ = patched(monkeypatch)

    @c.event
    async def on_ready():
        return None

    assert c._listeners["ready"] == [on_ready]


def test_event_decorator_strips_on_prefix(monkeypatch):
    c, gw = patched(monkeypatch)
    seen = {}

    @c.event
    async def on_message(message):
        seen["id"] = message.id
        seen["author"] = message.author.id

    payload = {
        "id": "m1",
        "channel_id": "c1",
        "content": "hi",
        "author": {"id": "u1", "username": "a"},
    }
    run(gw._emit("MESSAGE_CREATE", payload))
    assert seen == {"id": "m1", "author": "u1"}
    assert isinstance(gw.listeners["MESSAGE_CREATE"][-1].__name__, str)


def test_on_message_receives_a_model(monkeypatch):
    from despise.models import Message

    c, gw = patched(monkeypatch)
    seen = {}
    c.on("message", lambda m: seen.setdefault("m", m))
    payload = {
        "id": "m2",
        "channel_id": "c1",
        "content": "hey",
        "author": {"id": "u2", "username": "b"},
    }
    run(gw._emit("MESSAGE_CREATE", payload))
    assert isinstance(seen["m"], Message)
    assert seen["m"].content == "hey"
    assert seen["m"].author.id == "u2"


def test_on_raw_name_still_gets_the_raw_dict(monkeypatch):
    c, gw = patched(monkeypatch)
    seen = {}
    c.on("MESSAGE_CREATE", lambda d: seen.setdefault("d", d))
    payload = {"id": "m3", "author": {"id": "u3"}}
    run(gw._emit("MESSAGE_CREATE", payload))
    assert seen["d"] == payload


def test_wait_for_message_passes_a_model(monkeypatch):
    c, gw = patched(monkeypatch)

    async def main():
        waiter = asyncio.ensure_future(
            c.wait_for("message", check=lambda m: m.author.id == "u9")
        )
        await asyncio.sleep(0)
        await gw._emit(
            "MESSAGE_CREATE",
            {"id": "no", "author": {"id": "u1", "username": "x"}},
        )
        await gw._emit(
            "MESSAGE_CREATE",
            {"id": "yes", "author": {"id": "u9", "username": "y"}},
        )
        return await asyncio.wait_for(waiter, 1)

    found = run(main())
    assert found.id == "yes"


def test_on_error_registration(monkeypatch):
    c, _ = patched(monkeypatch)

    async def handler(event, exc):
        pass

    c.on_error(handler)
    assert c.gateway.error_listeners == [handler]


def test_dispatch_routes_through_emit(monkeypatch):
    c, gw = patched(monkeypatch)
    seen = {}
    c.on("message", lambda m: seen.setdefault("m", m))
    run(c.dispatch("MESSAGE_CREATE", {"id": "m1", "author": {"id": "u1"}}))
    assert seen["m"].id == "m1"


def test_wait_until_ready_with_timeout(monkeypatch):
    c, gw = patched(monkeypatch)

    async def main():
        await c.start()
        await c.wait_until_ready(timeout=0.5)
        assert c.is_ready

    run(main())


def test_wait_until_ready_times_out_when_never_ready(monkeypatch):
    c, gw = patched(monkeypatch)

    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await c.wait_until_ready(timeout=0.05)

    run(main())


# -- constructor passthrough ------------------------------------------------
def test_constructor_options_reach_the_profile():
    c = Client(
        "tok",
        profile="desktop",
        locale="fr-FR",
        timezone="Europe/Paris",
        hardware_concurrency=4,
        screen_resolution="1280x720",
        device_memory=16,
    )
    assert c.profile.locale == "fr-FR"
    assert c.profile.timezone == "Europe/Paris"
    assert c.profile.fingerprint["hardware_concurrency"] == 4
    assert c.profile.fingerprint["screen_resolution"] == "1280x720"
    assert c.profile.fingerprint["device_memory"] == 16
    run(c.http.close())


def test_api_base_override_reaches_http():
    c = Client("tok", api_base="https://discord.com/api/canary")
    assert c.http.api_base == "https://discord.com/api/canary"
    run(c.http.close())


# -- end-to-end over a fake socket -----------------------------------------
class Wire:
    """A fake transport that satisfies both the WS and the REST side."""

    def __init__(self, ws):
        self.ws = ws
        self.urls = []

    def ws_connect(self, url):
        self.urls.append(url)
        from test_gateway import FakeCtx

        return FakeCtx(self.ws)

    async def request(self, method, url, **kwargs):
        from test_http import FakeResp

        if url.endswith("/gateway"):
            return FakeResp(200, '{"url": "wss://gateway.discord.gg"}')
        raise AssertionError(f"unexpected REST call: {method} {url}")

    async def close(self):
        pass


def test_readme_example_end_to_end():
    """The documented usage must actually stay connected."""
    from test_gateway import FakeWS, hello, ready

    ws = FakeWS([hello(), ready()])
    # FakeWS feeds plain dict frames, so this test pins json. The default is
    # zlib-stream (covered by test_gateway_encoding_default_is_zlib_stream).
    bot = Client("tok", profile="web", gateway_encoding="json")
    bot.http.session = Wire(ws)  # type: ignore[assignment]

    async def fake_boot():
        return None

    bot.boot = fake_boot  # type: ignore[assignment]

    @bot.event
    async def on_ready():
        pass

    async def main():
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.05)
        assert not task.done(), "run() returned while still connected"
        assert bot.user is not None and bot.user.id == "9"
        assert "ready" in bot._listeners
        await bot.wait_until_ready(timeout=1)
        await bot.set_presence([{"name": "x", "type": 0}], force=True)
        assert any(p["op"] == 3 for p in ws.sent)
        await bot.close()
        await asyncio.wait_for(task, 2)  # returns cleanly

    run(main())
    assert bot.closed is True
    assert bot.http.session.urls == ["wss://gateway.discord.gg?v=9&encoding=json"]


def test_proxy_and_timeout_are_reachable():
    """They existed on DespiseHttp but no Client path forwarded them."""
    c = Client("tok", proxy="http://127.0.0.1:8080", timeout=12.0)
    assert c.http.session.proxies["all"] == "http://127.0.0.1:8080"
    assert c.http.session.timeout == 12.0
    run(c.http.close())


# -- gateway encoding / session cache --------------------------------------
def test_gateway_encoding_default_is_zlib_stream():
    c = Client("tok")
    assert c.gateway.encoding == "zlib-stream"
    run(c.http.close())


def test_gateway_encoding_reaches_the_socket():
    c = Client("tok", gateway_encoding="json")
    assert c.gateway.encoding == "json"
    run(c.http.close())


def test_bad_gateway_encoding_rejected_early():
    with pytest.raises(ValueError, match="unknown gateway_encoding"):
        Client("tok", gateway_encoding="brotli")


def test_session_cache_off_by_default():
    c = Client("tok")
    assert c.session_cache is None
    assert c.gateway.session_cache is None
    run(c.http.close())


def test_session_cache_enabled_in_memory():
    c = Client("tok", session_cache=True)
    assert c.session_cache is not None
    assert c.gateway.session_cache is c.session_cache
    run(c.close())


def test_session_cache_accepts_a_backend_url():
    c = Client("tok", session_cache="mem://?size=50")
    assert c.session_cache is not None
    run(c.close())


def test_close_releases_the_session_cache():
    from despise import session_cache as mod

    mod._shared.clear()
    c = Client("tok", session_cache=True)
    cache = c.session_cache
    run(c.close())
    assert cache._closed is True
    assert c.session_cache is None


def test_two_clients_same_token_share_one_cache():
    from despise import session_cache as mod

    mod._shared.clear()
    a = Client("tok-shared", session_cache=True)
    b = Client("tok-shared", session_cache=True)
    assert a.session_cache is b.session_cache
    run(a.close())
    run(b.close())


# -- READY seeds the cache (live payload shape) -------------------------------
def test_ready_seeds_guilds_and_normalizes_int_ids(monkeypatch):
    """Live READY embeds full guilds and uses integer snowflakes.

    Without seeding, every get_guild after connect burns a REST call.
    """
    c, gw = patched(monkeypatch)
    payload = {
        "session_id": "sess-live",
        "resume_gateway_url": "wss://gateway-us-east1-c.discord.gg",
        "user": {"id": 975049360167563334, "username": "conorbx113678"},
        "guilds": [
            {
                "id": 1550551193908936745,
                "name": "fan club",
                "owner_id": 975049360167563334,
                "roles": [{"id": 1550551193908936745, "name": "@everyone"}],
                "channels": [{"id": 1550551194672435203, "name": "general", "type": 0}],
                "members": [
                    {
                        "nick": None,
                        "roles": [],
                        "user": {"id": 975049360167563334, "username": "conorbx113678"},
                    }
                ],
            },
            {"id": "999", "unavailable": True},
        ],
        "private_channels": [{"id": 915532208574455828, "type": 1}],
    }
    run(gw._emit("READY", payload))
    guild = c.cache.get_guild("1550551193908936745")
    assert guild is not None and guild.name == "fan club"
    assert c.cache.get_channel("1550551194672435203") is not None
    assert c.cache.get_channel("915532208574455828") is not None
    assert c.cache.get_guild("999") is None
