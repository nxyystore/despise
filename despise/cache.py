"""Lightweight id -> model cache.

Deliberately *not* a member cache. Guild objects and channel lists are small
and are re-sent constantly by the gateway, so holding the latest copy saves a
round trip on every ``get_channel``. Users are cached because message authors
show up in almost every payload and re-fetching each one would be absurd.

Bounded per type (insertion-ordered dict, oldest evicted first) so a bot in
large guilds cannot grow without limit.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from .models import Channel, Guild, Member, User

log = logging.getLogger("despise.cache")

DEFAULT_GUILD_CAP = 512
DEFAULT_CHANNEL_CAP = 2048
DEFAULT_USER_CAP = 4096


class _Bounded:
    """Insertion-ordered id -> model map with a hard cap."""

    __slots__ = ("_data", "_cap")

    def __init__(self, cap: int) -> None:
        self._data: OrderedDict[str, object] = OrderedDict()
        self._cap = cap

    def put(self, key: str, value) -> None:
        if not key:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self._cap:
            self._data.popitem(last=False)

    def get(self, key: str):
        return self._data.get(key)

    def pop(self, key: str):
        return self._data.pop(key, None)

    def drop_prefix(self, prefix: str) -> int:
        """Remove every key starting with ``prefix`` (used for guild-scoped maps)."""
        doomed = [k for k in self._data if k.startswith(prefix)]
        for key in doomed:
            del self._data[key]
        return len(doomed)

    def keys(self):
        return self._data.keys()

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data


class EntityCache:
    """Holds the most recent copy of the entities we are commonly asked for.

    Never a source of truth: ``Client.get_*`` fetches through REST on a miss
    and falls back to the cache only when the request fails.
    """

    def __init__(
        self,
        *,
        guild_cap: int = DEFAULT_GUILD_CAP,
        channel_cap: int = DEFAULT_CHANNEL_CAP,
        user_cap: int = DEFAULT_USER_CAP,
    ) -> None:
        self.guilds = _Bounded(guild_cap)
        self.channels = _Bounded(channel_cap)
        self.users = _Bounded(user_cap)
        self.members = _Bounded(user_cap)

    # -- writes ---------------------------------------------------------
    def put_guild(self, guild: Guild) -> None:
        self.guilds.put(guild.id, guild)
        # GUILD_CREATE ships the whole channel list, and get_channel() is
        # usually the first thing called after joining. Index them so the
        # cache is immediately useful. Members are deliberately not indexed
        # here -- a large guild would flood the user cache.
        for channel in guild.channels:
            if not channel.guild_id:
                channel.guild_id = guild.id
            self.channels.put(channel.id, channel)

    def put_channel(self, channel: Channel) -> None:
        self.channels.put(channel.id, channel)
        # Guild objects embed their channel list; keep it addressable and in
        # sync. A CHANNEL_UPDATE carries the new state, so an existing entry
        # is replaced rather than skipped -- otherwise the cached guild would
        # keep serving a stale name/topic forever.
        if channel.guild_id:
            cached = self.guilds.get(channel.guild_id)
            if isinstance(cached, Guild):
                for index, existing in enumerate(cached.channels):
                    if existing.id == channel.id:
                        cached.channels[index] = channel
                        return
                cached.channels.append(channel)

    def put_user(self, user: User) -> None:
        self.users.put(user.id, user)

    def put_member(self, member: Member) -> None:
        if member.user is not None:
            self.put_user(member.user)
        key = f"{member.guild_id}:{member.id}"
        self.members.put(key, member)

    def put_message_author(self, user: User) -> None:
        self.put_user(user)

    def drop_guild(self, guild_id: str) -> None:
        guild = self.guilds.pop(guild_id)
        if isinstance(guild, Guild):
            for channel in guild.channels:
                self.channels.pop(channel.id)
        self.members.drop_prefix(f"{guild_id}:")

    def drop_channel(self, channel_id: str) -> None:
        self.channels.pop(channel_id)

    def clear(self) -> None:
        self.guilds.clear()
        self.channels.clear()
        self.users.clear()
        self.members.clear()

    # -- reads ----------------------------------------------------------
    def get_guild(self, guild_id: str):
        return self.guilds.get(str(guild_id))

    def get_channel(self, channel_id: str):
        return self.channels.get(str(channel_id))

    def get_user(self, user_id: str):
        return self.users.get(str(user_id))

    def get_member(self, user_id: str, guild_id: str):
        return self.members.get(f"{guild_id}:{user_id}")

    def __len__(self) -> int:
        return len(self.guilds) + len(self.channels) + len(self.users)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<EntityCache guilds={len(self.guilds)} channels={len(self.channels)} "
            f"users={len(self.users)}>"
        )
