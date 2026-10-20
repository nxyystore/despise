"""Session cache: round-trip, per-token isolation, TTL, throttling, sharing."""

import asyncio

import pytest

from despise.session_cache import (
    KEY_PREFIX,
    SessionCache,
    SessionState,
    for_token,
    release,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_registry():
    from despise import session_cache as mod

    mod._shared.clear()
    yield
    mod._shared.clear()


def test_save_and_load_roundtrip():
    cache = SessionCache("tok-A", ttl=60)

    async def main():
        assert await cache.load() is None
        assert await cache.save("sess-1", 42, "wss://resume") is True
        state = await cache.load()
        await cache.close()
        return state

    state = run(main())
    assert isinstance(state, SessionState)
    assert state.session_id == "sess-1"
    assert state.seq == 42
    assert state.resume_url == "wss://resume"


def test_key_is_namespaced_and_token_scoped():
    cache = SessionCache("tok-A")
    assert cache.key == f"{KEY_PREFIX}:tok-A"
    run(cache.close())


def test_tokens_are_isolated():
    """Never key by user id alone: a swapped token must not resume the old session."""
    a, b = SessionCache("tok-A"), SessionCache("tok-B")

    async def main():
        await a.save("sess-A", 1)
        await b.save("sess-B", 2)
        got_a, got_b = await a.load(), await b.load()
        await a.close()
        await b.close()
        return got_a, got_b

    got_a, got_b = run(main())
    assert got_a.session_id == "sess-A"
    assert got_b.session_id == "sess-B"


def test_save_is_throttled_unless_forced():
    cache = SessionCache("tok-A", min_save_interval=3600)

    async def main():
        first = await cache.save("sess-1", 1)
        second = await cache.save("sess-2", 2)
        forced = await cache.save("sess-3", 3, force=True)
        state = await cache.load()
        await cache.close()
        return first, second, forced, state

    first, second, forced, state = run(main())
    assert first is True
    assert second is False, "seq advances per dispatch; writes must be throttled"
    assert forced is True
    assert state.session_id == "sess-3"


def test_seq_updates_within_the_window_are_kept_latest():
    cache = SessionCache("tok-A", min_save_interval=3600)

    async def main():
        await cache.save("sess-1", 1, force=True)
        # throttled out, so the cached seq stays at the last forced write
        await cache.save("sess-1", 99)
        state = await cache.load()
        await cache.close()
        return state

    assert run(main()).seq == 1


def test_ttl_expires_the_session():
    """An abandoned token must not resurrect a week-old session."""
    cache = SessionCache("tok-A", ttl=0.2)

    async def main():
        await cache.save("sess-1", 1)
        assert (await cache.load()) is not None
        await asyncio.sleep(0.4)
        after = await cache.load()
        await cache.close()
        return after

    assert run(main()) is None


def test_clear_forces_a_reidentify():
    cache = SessionCache("tok-A")

    async def main():
        await cache.save("sess-1", 1)
        await cache.clear()
        after = await cache.load()
        await cache.close()
        return after

    assert run(main()) is None


def test_save_requires_a_session_id():
    cache = SessionCache("tok-A")

    async def main():
        out = await cache.save("", 1)
        await cache.close()
        return out

    assert run(main()) is False


def test_load_ignores_a_malformed_payload():
    cache = SessionCache("tok-A")

    async def main():
        await cache._cache.set(cache.key, "not-a-dict", expire=60)
        junk = await cache.load()
        await cache._cache.set(cache.key, {"seq": 5}, expire=60)  # no session_id
        no_id = await cache.load()
        await cache.close()
        return junk, no_id

    junk, no_id = run(main())
    assert junk is None
    assert no_id is None


def test_cache_errors_degrade_instead_of_raising():
    """A broken cache must never take the gateway down."""

    async def main():
        cache = SessionCache("tok-A")

        async def boom(*a, **kw):
            raise RuntimeError("redis down")

        cache._cache.get = boom
        cache._cache.set = boom
        cache._cache.delete = boom
        assert await cache.load() is None
        assert await cache.save("s", 1) is False
        await cache.clear()  # must not raise
        await cache.close()

    run(main())


def test_for_token_shares_between_clients_with_one_token():
    a = for_token("tok-SHARED")
    b = for_token("tok-SHARED")
    c = for_token("tok-OTHER")
    assert a is b
    assert a is not c

    async def main():
        await a.save("sess-1", 5)
        state = await b.load()
        await c.close()
        return state

    assert run(main()).session_id == "sess-1"


def test_for_token_builds_from_a_backend_url():
    cache = for_token("tok-R", backend="mem://?size=10")
    assert cache.key.endswith("tok-R")

    async def main():
        await cache.save("s", 1)
        state = await cache.load()
        await cache.close()
        return state

    assert run(main()).session_id == "s"


def test_release_keeps_a_shared_cache_open_until_the_last_client():
    a = for_token("tok-SHARED")
    b = for_token("tok-SHARED")

    async def main():
        await release(a)
        assert not b._closed
        await b.save("sess-2", 2)
        await release(b)
        return b._closed

    assert run(main()) is True
    assert for_token("tok-SHARED") is not a


def test_no_extra_needed_for_builtin_cache():
    """Regression: the cache used to require `pip install despise[session-cache]`."""
    import despise.session_cache as mod

    assert not hasattr(mod, "_INSTALL_HINT")
    cache = SessionCache("tok-X")

    async def main():
        assert await cache.save("s", 1) is True
        state = await cache.load()
        await cache.close()
        return state

    assert run(main()).session_id == "s"


def test_redis_url_parses_without_extra_deps():
    from despise.session_cache import _RedisBackend

    cache = SessionCache("tok-R", backend="redis://127.0.0.1:6379/0")
    assert isinstance(cache._cache, _RedisBackend)
    assert cache._cache.host == "127.0.0.1"
    assert cache._cache.port == 6379
    run(cache.close())
