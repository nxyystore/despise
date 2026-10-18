"""Friendly event names and payload conversion.

The gateway speaks raw dispatch names (``MESSAGE_CREATE``) with raw dicts.
That is precise but awkward to live in. This module maps those dispatches
onto the ``discord.py-self`` style names people already know (``message``,
``guild_join``) and converts payloads into models where a model exists.

Rules, kept small on purpose:

- One argument per event. ``on_message(message)``, ``on_ready()``.
- ``on_ready`` and ``on_resume`` take no arguments. Everything else takes one.
- Raw names keep working. ``bot.on("MESSAGE_CREATE", ...)`` gets the raw
  dict, exactly as before.
- Unmapped dispatches work too. ``bot.on("MESSAGE_POLL_VOTE_ADD", ...)`` (or
  ``@bot.event async def on_message_poll_vote_add``) fires even though no
  friendly name exists; the handler receives the raw dict.
- Conversion never raises. A payload that fails to convert arrives raw.
"""

from __future__ import annotations

import re
from typing import Any

# Raw dispatch -> friendly name.
RAW_TO_FRIENDLY: dict[str, str] = {
    "READY": "ready",
    "RESUMED": "resume",
    "MESSAGE_CREATE": "message",
    "MESSAGE_UPDATE": "message_edit",
    "MESSAGE_DELETE": "message_delete",
    "GUILD_CREATE": "guild_join",
    "GUILD_UPDATE": "guild_update",
    "GUILD_DELETE": "guild_remove",
    "CHANNEL_CREATE": "channel_create",
    "CHANNEL_UPDATE": "channel_update",
    "CHANNEL_DELETE": "channel_delete",
    "GUILD_MEMBER_ADD": "member_join",
    "GUILD_MEMBER_UPDATE": "member_update",
    "GUILD_MEMBER_REMOVE": "member_remove",
    "TYPING_START": "typing",
    "MESSAGE_REACTION_ADD": "reaction_add",
    "MESSAGE_REACTION_REMOVE": "reaction_remove",
    "MESSAGE_REACTION_REMOVE_ALL": "reaction_clear",
    "MESSAGE_REACTION_REMOVE_EMOJI": "reaction_clear_emoji",
    "PRESENCE_UPDATE": "presence_update",
    "VOICE_STATE_UPDATE": "voice_state",
    "INTERACTION_CREATE": "interaction",
    "RELATIONSHIP_ADD": "relationship_add",
    "RELATIONSHIP_REMOVE": "relationship_remove",
    "INVITE_CREATE": "invite_create",
    "INVITE_DELETE": "invite_delete",
    "GUILD_MEMBERS_UPDATE": "members_chunk",
    # -- coverage: the dispatches selfbot code reaches for next ---------
    "MESSAGE_DELETE_BULK": "message_delete_bulk",
    "CHANNEL_PINS_UPDATE": "channel_pins_update",
    "CHANNEL_UNREAD_UPDATE": "channel_unread_update",
    "CHANNEL_RECIPIENT_ADD": "recipient_add",
    "CHANNEL_RECIPIENT_REMOVE": "recipient_remove",
    "GUILD_ROLE_CREATE": "role_create",
    "GUILD_ROLE_UPDATE": "role_update",
    "GUILD_ROLE_DELETE": "role_delete",
    "GUILD_BAN_ADD": "member_ban",
    "GUILD_BAN_REMOVE": "member_unban",
    "GUILD_EMOJIS_UPDATE": "guild_emojis_update",
    "GUILD_STICKERS_UPDATE": "guild_stickers_update",
    "THREAD_CREATE": "thread_create",
    "THREAD_UPDATE": "thread_update",
    "THREAD_DELETE": "thread_delete",
    "USER_UPDATE": "user_update",
    "MESSAGE_ACK": "message_ack",
}

# Friendly alias -> raw dispatch. Built from the table above plus the
# spellings people reach for first.
FRIENDLY_TO_RAW: dict[str, str] = {v: k for k, v in RAW_TO_FRIENDLY.items()}
FRIENDLY_TO_RAW.update(
    {
        "messagecreate": "MESSAGE_CREATE",
        "messageupdate": "MESSAGE_UPDATE",
        "messagedelete": "MESSAGE_DELETE",
        "guildcreate": "GUILD_CREATE",
        "guilddelete": "GUILD_DELETE",
        "msg": "MESSAGE_CREATE",
    }
)

# Events whose listeners take no arguments.
NO_ARG_EVENTS = frozenset({"ready", "resume"})


def normalize(name: str) -> str:
    """``on_message`` -> ``message``. ``MESSAGE_CREATE`` -> ``message``.

    Aliases canonicalise too: ``msg`` -> ``message``. That matters because
    listeners are bucketed by the canonical name -- a non-canonical bucket is
    never filled by the dispatch bridge.
    """
    n = name.strip().lower()
    if n.startswith("on_"):
        n = n[3:]
    squashed = n.replace(" ", "")
    if squashed in FRIENDLY_TO_RAW:
        return normalize_friendly(squashed)
    noscore = squashed.replace("_", "")
    if noscore in FRIENDLY_TO_RAW:
        return normalize_friendly(noscore)
    upper = name.strip().upper().replace(" ", "_")
    if upper in RAW_TO_FRIENDLY:
        return RAW_TO_FRIENDLY[upper]
    return n


def normalize_friendly(squashed: str) -> str:
    raw = FRIENDLY_TO_RAW[squashed]
    return RAW_TO_FRIENDLY[raw]


def raw_for(name: str) -> str | None:
    """The gateway dispatch behind a friendly or raw name, if any."""
    n = name.strip()
    upper = n.upper().replace(" ", "_")
    if upper in RAW_TO_FRIENDLY:
        return upper
    norm = normalize(n)
    return FRIENDLY_TO_RAW.get(norm)


# Discord dispatches are SCREAMING_SNAKE_CASE with at least one underscore.
# Recognising the shape lets an unmapped dispatch (GUILD_BAN_ADD,
# THREAD_CREATE, ...) route correctly instead of silently landing in a
# friendly bucket nothing ever fills.
_DISPATCH_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def looks_like_dispatch(name: str) -> bool:
    return bool(_DISPATCH_RE.match(name))


def route(name: str) -> tuple[str, str, str | None]:
    """Resolve an event name to ``(kind, key, raw_dispatch)``.

    ``kind`` is ``"raw"`` or ``"friendly"``; ``key`` is the bucket the
    listener/waiters belong in; ``raw_dispatch`` is the gateway name to
    bridge (``None`` when nothing on the wire can trigger it).

    An exact dispatch name (``MESSAGE_CREATE``, and lower-case spellings of
    it) stays raw; a name that maps to a friendly event (``message``,
    ``on_message``, ``msg``) stays friendly. Anything else shaped like a
    dispatch is treated as raw, so a new Discord event is reachable the day
    it ships with no table edit -- raw names were always supposed to keep
    working.
    """
    stripped = name.strip()
    upper = stripped.upper().replace(" ", "_")
    if upper in RAW_TO_FRIENDLY:
        return "raw", upper, upper
    raw = raw_for(stripped)
    if raw is not None:
        return "friendly", normalize(stripped), raw
    # ``on_guild_ban_add`` (decorator) and ``guild_ban_add`` both mean the
    # GUILD_BAN_ADD dispatch; drop the ``on_`` prefix before deciding shape.
    candidate = upper[3:] if upper.startswith("ON_") else upper
    if looks_like_dispatch(candidate):
        return "raw", candidate, candidate
    return "friendly", normalize(stripped), None


def convert(raw: str, payload: Any) -> tuple[str | None, Any]:
    """Map a gateway dispatch to ``(friendly_name, value)``.

    ``value`` is a model where one fits, else the raw payload. ``ready``
    and ``resume`` convert to ``None`` because their listeners take no
    arguments. Returns ``(None, payload)`` for dispatches with no mapping.
    """
    friendly = RAW_TO_FRIENDLY.get(raw)
    if friendly is None:
        return None, payload
    if friendly in NO_ARG_EVENTS:
        return friendly, None
    if not isinstance(payload, dict):
        return friendly, payload
    try:
        return friendly, _build(friendly, payload)
    except Exception:
        return friendly, payload


def _build(friendly: str, payload: dict) -> Any:
    from . import models as m

    if friendly in ("message", "message_edit", "message_delete"):
        return m.Message(payload)
    if friendly in ("guild_join", "guild_update"):
        return m.Guild(payload)
    if friendly == "guild_remove":
        if "id" in payload:
            return m.Guild(payload)
        if "guild_id" in payload:  # Discord nests the id here on removal
            return m.Guild({**payload, "id": payload["guild_id"]})
        return payload
    if friendly in ("channel_create", "channel_update", "channel_delete"):
        return m.Channel(payload)
    if friendly in ("thread_create", "thread_update", "thread_delete"):
        return m.Channel(payload)
    if friendly in ("role_create", "role_update"):
        # The role object is nested under ``role`` on create/update.
        role = payload.get("role")
        return m.Role(role) if isinstance(role, dict) else payload
    if friendly == "role_delete":
        # guild role delete ships ``role_id`` rather than ``id``.
        if "id" in payload:
            return m.Role(payload)
        if "role_id" in payload:
            return m.Role({**payload, "id": payload["role_id"]})
        return payload
    if friendly in ("member_ban", "member_unban"):
        user = payload.get("user")
        return m.User(user) if isinstance(user, dict) else payload
    if friendly == "user_update":
        return m.User(payload)
    if friendly in ("recipient_add", "recipient_remove"):
        user = payload.get("user")
        return m.User(user) if isinstance(user, dict) else payload
    if friendly in ("member_join", "member_update", "member_remove"):
        return m.Member(payload)
    if friendly == "voice_state":
        return m.VoiceState(payload)
    if friendly in ("relationship_add", "relationship_remove"):
        return m.Relationship(payload)
    if friendly in ("invite_create", "invite_delete"):
        return m.Invite(payload)
    if friendly in ("reaction_add", "reaction_remove"):
        return m.ReactionEvent(payload)
    # typing, presence_update, interaction, members_chunk: no single model
    # fits, so handlers receive the raw dict.
    return payload
