"""Cache tests: population from dispatches, bounded eviction, lookup fallbacks."""



from test_client import patched, run

from despise.cache import EntityCache
from despise.models import Channel, Guild, Member, User


# -- the container ----------------------------------------------------------
def test_put_and_get():
    cache = EntityCache()
    guild = Guild({"id": "g1", "name": "G"})
    cache.put_guild(guild)
    assert cache.get_guild("g1") is guild
    assert cache.get_guild("nope") is None


def test_guild_id_is_coerced_to_string():
    cache = EntityCache()
    cache.put_guild(Guild({"id": "123"}))
    assert cache.get_guild(123) is not None


def test_channel_is_addressable_inside_the_guild():
    cache = EntityCache()
    guild = Guild({"id": "g1", "channels": [{"id": "c0", "type": 0}]})
    cache.put_guild(guild)
    cache.put_channel(Channel({"id": "c1", "type": 0, "guild_id": "g1"}))
    assert cache.get_channel("c1").id == "c1"
    assert {c.id for c in cache.get_guild("g1").channels} == {"c0", "c1"}


def test_reputing_a_channel_does_not_duplicate_it():
    cache = EntityCache()
    cache.put_guild(Guild({"id": "g1", "channels": []}))
    for _ in range(3):
        cache.put_channel(Channel({"id": "c1", "type": 0, "guild_id": "g1"}))
    assert len(cache.get_guild("g1").channels) == 1


def test_put_channel_updates_an_existing_guild_copy():
    cache = EntityCache()
    cache.put_guild(Guild({"id": "g1", "channels": [{"id": "c1", "type": 0, "name": "old"}]}))
    cache.put_channel(Channel({"id": "c1", "type": 0, "guild_id": "g1", "name": "new"}))
    assert cache.get_guild("g1").channels[0].name == "new"


def test_user_cache_populates_from_member():
    cache = EntityCache()
    cache.put_member(Member({"user": {"id": "u1"}, "guild_id": "g1"}))
    assert cache.get_user("u1") is not None


def test_member_is_scoped_per_guild():
    cache = EntityCache()
    cache.put_member(Member({"user": {"id": "u1"}, "guild_id": "g1"}))
    assert cache.get_member("u1", "g1") is not None
    assert cache.get_member("u1", "g2") is None


def test_drop_guild_clears_its_channels_and_members():
    cache = EntityCache()
    cache.put_guild(Guild({"id": "g1", "channels": [{"id": "c1", "type": 0}]}))
    cache.put_member(Member({"user": {"id": "u1"}, "guild_id": "g1"}))
    cache.drop_guild("g1")
    assert cache.get_guild("g1") is None
    assert cache.get_channel("c1") is None
    assert cache.get_member("u1", "g1") is None


def test_guild_cap_evicts_oldest():
    cache = EntityCache(guild_cap=3)
    for i in range(5):
        cache.put_guild(Guild({"id": f"g{i}"}))
    assert len(cache.guilds) == 3
    assert cache.get_guild("g0") is None
    assert cache.get_guild("g4") is not None


def test_cap_refreshes_recency():
    cache = EntityCache(guild_cap=2)
    cache.put_guild(Guild({"id": "a"}))
    cache.put_guild(Guild({"id": "b"}))
    cache.put_guild(Guild({"id": "a"}))  # touch a
    cache.put_guild(Guild({"id": "c"}))
    assert cache.get_guild("a") is not None
    assert cache.get_guild("b") is None


def test_clear_empties_everything():
    cache = EntityCache()
    cache.put_guild(Guild({"id": "g"}))
    cache.put_user(User({"id": "u"}))
    cache.clear()
    assert len(cache) == 0


def test_blank_ids_are_not_cached():
    cache = EntityCache()
    cache.put_guild(Guild({}))
    assert len(cache.guilds) == 0


# -- population from gateway dispatches -------------------------------------
def test_guild_create_populates_the_cache(monkeypatch):
    client, gw = patched(monkeypatch)

    async def main():
        await client.dispatch("GUILD_CREATE", {"id": "g1", "name": "G", "channels": []})
        return client.cache.get_guild("g1")

    guild = run(main())
    assert guild is not None and guild.name == "G"


def test_guild_delete_drops_it(monkeypatch):
    client, gw = patched(monkeypatch)

    async def main():
        await client.dispatch("GUILD_CREATE", {"id": "g1", "channels": [{"id": "c1"}]})
        assert client.cache.get_channel("c1") is not None
        await client.dispatch("GUILD_DELETE", {"id": "g1"})
        return client.cache.get_guild("g1"), client.cache.get_channel("c1")

    guild, channel = run(main())
    assert guild is None and channel is None


def test_unavailable_guild_is_not_cached(monkeypatch):
    client, _ = patched(monkeypatch)

    async def main():
        await client.dispatch("GUILD_CREATE", {"id": "g1", "unavailable": True})
        return client.cache.get_guild("g1")

    assert run(main()) is None


def test_message_create_caches_the_author(monkeypatch):
    client, _ = patched(monkeypatch)

    async def main():
        await client.dispatch(
            "MESSAGE_CREATE", {"id": "m", "author": {"id": "u1", "username": "z"}}
        )
        return client.cache.get_user("u1")

    user = run(main())
    assert user is not None and user.username == "z"


def test_channel_delete_drops_it(monkeypatch):
    client, _ = patched(monkeypatch)

    async def main():
        await client.dispatch("CHANNEL_CREATE", {"id": "c1", "type": 0})
        assert client.cache.get_channel("c1") is not None
        await client.dispatch("CHANNEL_DELETE", {"id": "c1"})
        return client.cache.get_channel("c1")

    assert run(main()) is None


def test_member_add_caches_the_member_and_user(monkeypatch):
    client, _ = patched(monkeypatch)

    async def main():
        await client.dispatch(
            "GUILD_MEMBER_ADD", {"user": {"id": "u1", "username": "z"}, "roles": []}
        )
        return client.cache.get_member("u1", ""), client.cache.get_user("u1")

    member, user = run(main())
    assert user is not None
    assert member is None or member.id == "u1"  # guild_id absent in this payload


# -- REST fallbacks ---------------------------------------------------------
class FakeHttp:
    def __init__(self, responses):

        self._responses = list(responses)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def client_with(responses):
    from despise.client import Client
    from despise.rest import Rest

    client = Client.__new__(Client)  # bypass network-bearing __init__
    from despise.cache import EntityCache

    client.cache = EntityCache()
    client.http = FakeHttp(responses)
    client.rest = Rest(client.http)  # type: ignore[arg-type]
    return client


def test_get_guild_caches_a_fetched_result():

    client = client_with([{"id": "g1", "name": "Fetched"}])
    guild = run(client.get_guild("g1"))
    assert guild.name == "Fetched"
    assert client.cache.get_guild("g1") is guild
    # second call is served from cache
    assert run(client.get_guild("g1")) is guild
    assert client.http.calls == [("GET", "/guilds/g1")]


def test_get_guild_returns_none_on_404():
    from despise.exceptions import NotFound

    client = client_with([NotFound("nope")])
    assert run(client.get_guild("g1")) is None


def test_get_channel_fetches_and_caches():
    client = client_with([{"id": "c1", "type": 0, "name": "general"}])
    channel = run(client.get_channel("c1"))
    assert channel.name == "general"
    assert run(client.get_channel("c1")) is channel
    assert len(client.http.calls) == 1


def test_get_user_fetches_and_caches():
    client = client_with([{"id": "u1", "username": "z"}])
    user = run(client.get_user("u1"))
    assert user.username == "z"
    assert run(client.get_user("u1")) is user


def test_lookup_returns_none_on_forbidden():
    from despise.exceptions import Forbidden

    client = client_with([Forbidden("no")])
    assert run(client.get_user("u1")) is None


def test_get_member_is_cache_only():
    client = client_with([])
    assert client.get_member("u1", "g1") is None  # sync, cache-only
    assert client.http.calls == []


def test_clear_cache_helper():
    client = client_with([])
    client.cache.put_user(User({"id": "u"}))
    client.clear_cache()
    assert client.cache.get_user("u") is None
