"""Ready-made @require predicates.

Each builder returns a ``ctx -> bool`` predicate (async where a cache
lookup is involved). Import them from here::

    from despise.cmd.checks import in_guild, is_owner
"""

from __future__ import annotations

from typing import Any


def is_owner():
    """Only users in ``bot.owner_ids``. Empty when unset, so it fails closed."""

    async def owner_only(ctx: Any) -> bool:
        owners = {str(o) for o in getattr(ctx.bot, "owner_ids", ())}
        author = getattr(ctx.message, "author", None)
        return str(getattr(author, "id", "")) in owners

    return owner_only


def in_guild():
    """Only messages sent in a server (not DMs)."""

    def guild_only(ctx: Any) -> bool:
        return bool(str(getattr(ctx.message, "guild_id", "") or ""))

    return guild_only


def in_dm():
    """Only direct messages."""

    def dm_only(ctx: Any) -> bool:
        return not str(getattr(ctx.message, "guild_id", "") or "")

    return dm_only


def in_channel(*ids: Any):
    """Only the given channel IDs."""

    want = {str(i) for i in ids}

    def channel_only(ctx: Any) -> bool:
        return str(getattr(ctx.message, "channel_id", "") or "") in want

    return channel_only


def has_role(*ids: Any):
    """Only members holding one of the given role IDs.

    Reads roles off the message's member first, then the member cache.
    Never hits REST.
    """

    want = {str(i) for i in ids}

    def role_only(ctx: Any) -> bool:
        message = ctx.message
        member = getattr(message, "member", None)
        roles = list(getattr(member, "roles", None) or [])
        if not roles:
            guild_id = str(getattr(message, "guild_id", "") or "")
            author = getattr(message, "author", None)
            if guild_id and author is not None:
                member = ctx.bot.get_member(str(getattr(author, "id", "")), guild_id)
                roles = list(getattr(member, "roles", None) or []) if member else []
        return any(str(r) in want for r in roles)

    return role_only
