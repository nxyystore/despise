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
- Conversion never raises. A payload that fails to convert arrives raw.
"""

from __future__ import annotations

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
    "PRESENCE_UPDATE": "presence_update",
    "VOICE_STATE_UPDATE": "voice_state",
    "INTERACTION_CREATE": "interaction",
    "RELATIONSHIP_ADD": "relationship_add",
    "RELATIONSHIP_REMOVE": "relationship_remove",
    "INVITE_CREATE": "invite_create",
    "INVITE_DELETE": "invite_delete",
    "GUILD_MEMBERS_UPDATE": "members_chunk",
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
    """``on_message`` -> ``message``. ``MESSAGE_CREATE`` -> ``message``."""
    n = name.strip().lower()
    if n.startswith("on_"):
        n = n[3:]
    squashed = n.replace(" ", "")
    if squashed in FRIENDLY_TO_RAW:
        return squashed
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
    if friendly in ("member_join", "member_update", "member_remove"):
        return m.Member(payload)
    if friendly == "voice_state":
        return m.VoiceState(payload)
    if friendly in ("relationship_add", "relationship_remove"):
        return m.Relationship(payload)
    if friendly in ("invite_create", "invite_delete"):
        return m.Invite(payload)
    if friendly == "reaction_add" or friendly == "reaction_remove":
        emoji = payload.get("emoji")
        if isinstance(emoji, dict):
            return m.Reaction({"emoji": emoji, "count": 1, "me": False})
        return payload
    # typing, presence_update, interaction, members_chunk: no single model
    # fits, so handlers receive the raw dict.
    return payload
