"""Data models.

Design notes:

- Every model keeps its source dict in ``raw``. Anything Discord sends that
  is not modelled here is still reachable, which is what stops "the library
  doesn't expose X" from being a reason to abandon it.
- Equality and hashing are by snowflake id. Discord snowflakes are globally
  unique across entity types, so ``msg.author == bot.user`` works and no
  cross-type collision is possible.
- Timestamps are parsed to tz-aware ``datetime``. Discord sends ISO-8601 with
  a ``Z`` or explicit offset and up to microsecond precision; Python 3.10's
  ``fromisoformat`` rejects both ``Z`` and >6 fractional digits, so parsing
  goes through :func:`parse_timestamp` instead.
- ``__slots__`` throughout: models are created per message, so the memory
  difference is real.
"""

from __future__ import annotations

import re
from datetime import datetime

# Discord timestamps: 2024-01-02T03:04:05.123456+00:00 / ...Z
_TS_RE = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(value: object) -> datetime | None:
    """ISO-8601 -> tz-aware datetime, tolerating what Discord actually sends.

    Returns ``None`` for anything unparseable rather than raising: a single
    odd field should not break message construction.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    match = _TS_RE.match(value.strip())
    if match is None:
        return None
    frac = (match.group("frac") or "")[:6].ljust(6, "0")
    tz = match.group("tz")
    if not tz:
        tz = "+00:00"  # Discord always sends an offset; assume UTC if absent
    elif tz in ("Z", "z"):
        tz = "+00:00"
    elif ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    try:
        return datetime.fromisoformat(f"{match.group('head')}.{frac}{tz}")
    except ValueError:
        return None


class Model:
    """Base for every entity. Subclasses declare their own ``__slots__``."""

    __slots__ = ("_raw", "_id")

    def __init__(self, data: dict | None) -> None:
        payload = data if isinstance(data, dict) else {}
        self._raw = payload
        raw_id = payload.get("id")
        self._id = str(raw_id) if raw_id is not None else ""

    @property
    def id(self) -> str:
        """Snowflake id as a string (always string-typed, even when empty)."""
        return self._id

    @property
    def raw(self) -> dict:
        """The untouched Discord payload this model was built from."""
        return self._raw

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Model):
            return bool(self._id) and self._id == other._id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self._id}>"


def _child_list(payload: dict, key: str, cls: type) -> list:
    """Build a list of models, skipping malformed entries."""
    out = []
    for item in payload.get(key) or []:
        if isinstance(item, dict):
            try:
                out.append(cls(item))
            except Exception:  # pragma: no cover - defensive
                continue
    return out


def _nested(payload: dict, key: str, cls: type):
    value = payload.get(key)
    return cls(value) if isinstance(value, dict) else None


def _str(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


# -- people -----------------------------------------------------------------
class User(Model):
    __slots__ = (
        "username",
        "discriminator",
        "global_name",
        "avatar",
        "bot",
        "system",
        "public_flags",
        "accent_color",
        "display_name",
    )

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.username = payload.get("username", "")
        # Discord dropped discriminators for unique usernames; "0" is legacy.
        self.discriminator = payload.get("discriminator", "0")
        self.global_name = payload.get("global_name")
        self.avatar = payload.get("avatar")
        self.bot = payload.get("bot", False)
        self.system = payload.get("system", False)
        self.public_flags = payload.get("public_flags", 0)
        self.accent_color = payload.get("accent_color")
        self.display_name = self.global_name or self.username

    @property
    def avatar_url(self) -> str | None:
        if not self.avatar:
            return None
        ext = "gif" if self.avatar.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{self._id}/{self.avatar}.{ext}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} ({self._id})>"


class Member(Model):
    """A user's membership in one guild. ``user`` may be absent (lazy lists)."""

    __slots__ = (
        "guild_id",
        "user",
        "nick",
        "roles",
        "joined_at",
        "premium_since",
        "deaf",
        "mute",
        "pending",
        "flags",
        "communication_disabled_until",
        "avatar",
        "boosting_since",
    )

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.guild_id = str(payload.get("guild_id", ""))
        self.user = _nested(payload, "user", User)
        # Guild member lists key on the user id, not the member object's id.
        if not self._id and self.user is not None:
            self._id = self.user._id
        self.nick = payload.get("nick")
        self.roles = [str(r) for r in payload.get("roles") or []]
        self.joined_at = parse_timestamp(payload.get("joined_at"))
        self.premium_since = parse_timestamp(payload.get("premium_since"))
        self.boosting_since = self.premium_since
        self.deaf = payload.get("deaf", False)
        self.mute = payload.get("mute", False)
        self.pending = payload.get("pending", False)
        self.flags = payload.get("flags", 0)
        self.communication_disabled_until = parse_timestamp(
            payload.get("communication_disabled_until")
        )
        self.avatar = payload.get("avatar")

    @property
    def display_name(self) -> str:
        return self.nick or (self.user.display_name if self.user else self._id)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Member {self.display_name} in {self.guild_id}>"


class Relationship(Model):
    """A friend / blocked / incoming / outgoing relationship."""

    __slots__ = ("user", "type", "nickname", "since", "user_id")

    FRIEND = 1
    BLOCKED = 2
    INCOMING = 3
    OUTGOING = 4
    IMPLICIT = 5

    TYPE_NAMES = {
        1: "friend",
        2: "blocked",
        3: "incoming",
        4: "outgoing",
        5: "implicit",
    }

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.user = _nested(payload, "user", User)
        self.user_id = self.user._id if self.user else self._id
        if not self._id:
            self._id = self.user_id
        self.type = payload.get("type", 0)
        self.nickname = payload.get("nickname")
        self.since = parse_timestamp(payload.get("since"))

    @property
    def type_name(self) -> str:
        return self.TYPE_NAMES.get(self.type, "unknown")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Relationship {self.type_name} {self._id}>"


class Role(Model):
    __slots__ = (
        "name",
        "color",
        "hoist",
        "icon",
        "unicode_emoji",
        "position",
        "permissions",
        "managed",
        "mentionable",
        "tags",
    )

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.name = payload.get("name", "")
        self.color = payload.get("color", 0)
        self.hoist = payload.get("hoist", False)
        self.icon = payload.get("icon")
        self.unicode_emoji = payload.get("unicode_emoji")
        self.position = payload.get("position", 0)
        self.permissions = payload.get("permissions", "0")
        self.managed = payload.get("managed", False)
        self.mentionable = payload.get("mentionable", False)
        self.tags = payload.get("tags") or {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Role {self.name} ({self._id})>"


class Emoji(Model):
    __slots__ = ("name", "animated", "roles", "user", "require_colons", "managed", "available")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.name = payload.get("name") or ""
        self.animated = payload.get("animated", False)
        self.roles = [str(r) for r in payload.get("roles") or []]
        self.user = _nested(payload, "user", User)
        self.require_colons = payload.get("require_colons", True)
        self.managed = payload.get("managed", False)
        self.available = payload.get("available", True)

    @property
    def mention(self) -> str:
        return f"<:{self.name}:{self._id}>" if self._id else f"<:{self.name}>"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Emoji {self.name} ({self._id})>"


class Sticker(Model):
    __slots__ = ("name", "description", "tags", "format_type", "pack_id", "available", "user")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.name = payload.get("name", "")
        self.description = payload.get("description")
        self.tags = payload.get("tags") or ""
        self.format_type = payload.get("format_type")
        self.pack_id = str(payload.get("pack_id") or "")
        self.available = payload.get("available", True)
        self.user = _nested(payload, "user", User)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Sticker {self.name} ({self._id})>"


# -- content ----------------------------------------------------------------
class Attachment(Model):
    __slots__ = ("filename", "title", "description", "content_type", "size", "url",
                 "proxy_url", "height", "width", "ephemeral", "duration_secs",
                 "waveform", "flags")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.filename = payload.get("filename") or ""
        self.title = payload.get("title")
        self.description = payload.get("description")
        self.content_type = payload.get("content_type")
        self.size = payload.get("size", 0)
        self.url = payload.get("url")
        self.proxy_url = payload.get("proxy_url")
        self.height = payload.get("height")
        self.width = payload.get("width")
        self.ephemeral = payload.get("ephemeral", False)
        self.duration_secs = payload.get("duration_secs")
        self.waveform = payload.get("waveform")
        self.flags = payload.get("flags", 0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attachment {self.filename} ({self._id})>"


class EmbedField(Model):
    __slots__ = ("name", "value", "inline")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.name = payload.get("name", "")
        self.value = payload.get("value", "")
        self.inline = payload.get("inline", False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmbedField {self.name}>"


class Embed(Model):
    __slots__ = ("title", "description", "url", "color", "timestamp", "footer",
                 "image", "thumbnail", "video", "provider", "author", "fields", "type")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.title = payload.get("title")
        self.description = payload.get("description")
        self.url = payload.get("url")
        self.color = payload.get("color")
        self.timestamp = parse_timestamp(payload.get("timestamp"))
        self.footer = payload.get("footer") or {}
        self.image = payload.get("image") or {}
        self.thumbnail = payload.get("thumbnail") or {}
        self.video = payload.get("video") or {}
        self.provider = payload.get("provider") or {}
        self.author = payload.get("author") or {}
        self.type = payload.get("type")
        self.fields = [EmbedField(f) for f in payload.get("fields") or [] if isinstance(f, dict)]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Embed {self.title!r}>"


class Reaction(Model):
    __slots__ = ("count", "me", "emoji", "count_details", "burst_colors")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.count = payload.get("count", 0)
        self.me = payload.get("me", False)
        self.emoji = _nested(payload, "emoji", Emoji)
        details = payload.get("count_details")
        self.count_details = details if isinstance(details, dict) else {}
        self.burst_colors = payload.get("burst_colors") or []

    @property
    def name(self) -> str:
        return self.emoji.name if self.emoji else ""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Reaction {self.name} x{self.count}>"


class MessageReference(Model):
    __slots__ = ("message_id", "channel_id", "guild_id", "fail_if_not_exists")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.message_id = str(payload.get("message_id") or "")
        self.channel_id = str(payload.get("channel_id") or "")
        self.guild_id = str(payload.get("guild_id") or "")
        self.fail_if_not_exists = payload.get("fail_if_not_exists", True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MessageReference {self.message_id}>"


# Message flags (subset; see raw for the full set).
MESSAGE_FLAG_CROSSPOSTED = 1 << 0
MESSAGE_FLAG_IS_CROSSPOST = 1 << 1
MESSAGE_FLAG_SUPPRESS_EMBEDS = 1 << 2
MESSAGE_FLAG_SOURCE_MESSAGE_DELETED = 1 << 3
MESSAGE_FLAG_URGENT = 1 << 4
MESSAGE_FLAG_HAS_THREAD = 1 << 5
MESSAGE_FLAG_EPHEMERAL = 1 << 6
MESSAGE_FLAG_LOADING = 1 << 7
MESSAGE_FLAG_FAILED_TO_MENTION_SOME_ROLES = 1 << 8
MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12
MESSAGE_FLAG_IS_VOICE_MESSAGE = 1 << 13
MESSAGE_FLAG_COMPONENTS_V2 = 1 << 15

MESSAGE_TYPES = {
    0: "default",
    1: "recipient_add",
    2: "recipient_remove",
    3: "call",
    4: "channel_name_change",
    5: "channel_icon_change",
    6: "channel_pin_add",
    7: "user_join",
    8: "guild_boost",
    9: "guild_boost_tier_1",
    10: "guild_boost_tier_2",
    11: "guild_boost_tier_3",
    12: "channel_follow_add",
    13: "guild_discovery_disqualified",
    14: "guild_discovery_requalified",
    15: "thread_created",
    16: "reply",
    17: "chat_input_command",
    18: "thread_starter_message",
    19: "guild_invite_reminder",
    20: "context_menu_command",
    21: "auto_moderation_action",
    22: "role_subscription_purchase",
    23: "interaction_premium_upsell",
    24: "stage_start",
    25: "stage_end",
    26: "stage_speaker",
    27: "stage_raise_hand",
    28: "stage_topic",
    29: "guild_application_premium_subscription",
    30: "guild_incident_alert_mode_enabled",
    31: "guild_incident_alert_mode_disabled",
    32: "guild_incident_report_raid",
    33: "guild_incident_report_false_alarm",
    34: "purchase_notification",
    35: "poll_result",
}

# Message types that count toward a client's "messages sent" quota.
MESSAGE_TYPES_COUNTED = (0, 19)

_MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|@everyone|@here")
# Discord's clean_content also drops emphasis/spoiler markers.
_MARKDOWN_RE = re.compile(r"[*_~`|>]")


class Message(Model):
    __slots__ = (
        "channel_id",
        "guild_id",
        "author",
        "member",
        "content",
        "timestamp",
        "edited_timestamp",
        "tts",
        "mention_everyone",
        "mention",
        "mentions",
        "mention_roles",
        "mention_channels",
        "attachments",
        "embeds",
        "reactions",
        "reference",
        "referenced_message",
        "stickers",
        "components",
        "polls",
        "nonce",
        "flags",
        "type",
        "webhook_id",
        "application_id",
        "position",
        "activity",
        "interaction",
        "thread",
        "position_in_thread",
    )

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.channel_id = str(payload.get("channel_id", ""))
        self.guild_id = str(payload.get("guild_id") or "")
        self.author = _nested(payload, "author", User) or User(None)
        self.member = _nested(payload, "member", Member)
        self.content = payload.get("content", "") or ""
        self.timestamp = parse_timestamp(payload.get("timestamp"))
        self.edited_timestamp = parse_timestamp(payload.get("edited_timestamp"))
        self.tts = payload.get("tts", False)
        self.mention_everyone = payload.get("mention_everyone", False)
        self.mention = _nested(payload, "mention", User)
        self.mentions = _child_list(payload, "mentions", User)
        self.mention_roles = [str(r) for r in payload.get("mention_roles") or []]
        self.mention_channels = _child_list(payload, "mention_channels", Channel)
        self.attachments = _child_list(payload, "attachments", Attachment)
        self.embeds = _child_list(payload, "embeds", Embed)
        self.reactions = _child_list(payload, "reactions", Reaction)
        self.reference = _nested(payload, "message_reference", MessageReference)
        self.referenced_message = _nested(payload, "referenced_message", Message)
        self.stickers = _child_list(payload, "sticker_items", Sticker)
        self.components = payload.get("components") or []
        # Poll payloads are deeply nested and version-volatile; the raw list
        # is kept as-is rather than modelled speculatively.
        self.polls = payload.get("poll") or payload.get("polls") or []
        self.nonce = payload.get("nonce")
        self.flags = payload.get("flags", 0)
        self.type = payload.get("type", 0)
        self.webhook_id = str(payload.get("webhook_id") or "")
        self.application_id = str(payload.get("application_id") or "")
        self.position = payload.get("position")
        self.activity = payload.get("activity") or {}
        self.interaction = payload.get("interaction") or {}
        thread = payload.get("thread")
        self.thread = _nested({"id": (thread or {}).get("id")}, "id", Channel) if isinstance(thread, dict) else None
        self.position_in_thread = payload.get("position_in_thread")

    @property
    def clean_content(self) -> str:
        """``content`` with mentions, @everyone and markdown stripped."""
        return _clean_content(self.content)

    @property
    def type_name(self) -> str:
        return MESSAGE_TYPES.get(self.type, "unknown")

    @property
    def suppressed_embeds(self) -> bool:
        return bool(self.flags & MESSAGE_FLAG_SUPPRESS_EMBEDS)

    @property
    def is_components_v2(self) -> bool:
        return bool(self.flags & MESSAGE_FLAG_COMPONENTS_V2)

    @property
    def is_ephemeral(self) -> bool:
        return bool(self.flags & MESSAGE_FLAG_EPHEMERAL)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self._id} by {self.author.username}>"


def _clean_content(content: str) -> str:
    """Strip mentions and lightweight markdown, mirroring Discord's preview."""
    if not content:
        return ""
    out = _MENTION_RE.sub("", content)
    out = _MARKDOWN_RE.sub("", out)
    # collapse the whitespace left behind by removed mentions
    return " ".join(out.split()).strip()


# -- structure --------------------------------------------------------------
class Channel(Model):
    __slots__ = (
        "type",
        "guild_id",
        "name",
        "topic",
        "position",
        "parent_id",
        "recipients",
        "last_message_id",
        "permission_overwrites",
        "nsfw",
        "rate_limit_per_user",
        "icon",
        "owner_id",
        "thread_metadata",
        "message_count",
        "member_count",
    )

    GUILD_TEXT = 0
    DM = 1
    GUILD_VOICE = 2
    GROUP_DM = 3
    GUILD_CATEGORY = 4
    GUILD_ANNOUNCEMENT = 5
    GUILD_STORE = 6
    ANNOUNCEMENT_THREAD = 10
    PUBLIC_THREAD = 11
    PRIVATE_THREAD = 12
    GUILD_STAGE = 13
    GUILD_DIRECTORY = 14
    GUILD_FORUM = 15

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.type = payload.get("type", 0)
        self.guild_id = str(payload.get("guild_id") or "")
        self.name = payload.get("name") or ""
        self.topic = payload.get("topic")
        self.position = payload.get("position")
        self.parent_id = str(payload.get("parent_id") or "")
        self.recipients = _child_list(payload, "recipients", User)
        self.last_message_id = payload.get("last_message_id")
        overwrites = payload.get("permission_overwrites")
        self.permission_overwrites = overwrites if isinstance(overwrites, list) else []
        self.nsfw = payload.get("nsfw", False)
        self.rate_limit_per_user = payload.get("rate_limit_per_user")
        self.icon = payload.get("icon")
        self.owner_id = str(payload.get("owner_id") or "")
        meta = payload.get("thread_metadata")
        self.thread_metadata = meta if isinstance(meta, dict) else {}
        self.message_count = payload.get("message_count")
        self.member_count = payload.get("member_count")

    @property
    def is_dm(self) -> bool:
        return self.type in (self.DM, self.GROUP_DM)

    @property
    def is_thread(self) -> bool:
        return self.type in (
            self.ANNOUNCEMENT_THREAD,
            self.PUBLIC_THREAD,
            self.PRIVATE_THREAD,
        )

    @property
    def is_voice(self) -> bool:
        return self.type in (self.GUILD_VOICE, self.GUILD_STAGE)

    @property
    def mention(self) -> str:
        return f"#{self.name}" if not self.is_dm else self.name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Channel {self._id} type={self.type}>"


class Guild(Model):
    __slots__ = (
        "name",
        "icon",
        "banner",
        "owner_id",
        "member_count",
        "channels",
        "members",
        "roles",
        "emojis",
        "stickers",
        "features",
        "description",
        "preferred_locale",
        "vanity_url_code",
        "verification_level",
        "premium_tier",
        "joined_at",
        "large",
        "unavailable",
    )

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.name = payload.get("name", "")
        self.icon = payload.get("icon")
        self.banner = payload.get("banner")
        self.owner_id = str(payload.get("owner_id") or "")
        self.member_count = payload.get("member_count")
        self.channels = _child_list(payload, "channels", Channel)
        self.members = _child_list(payload, "members", Member)
        self.roles = _child_list(payload, "roles", Role)
        self.emojis = _child_list(payload, "emojis", Emoji)
        self.stickers = _child_list(payload, "sticker_items", Sticker)
        features = payload.get("features")
        self.features = features if isinstance(features, list) else []
        self.description = payload.get("description")
        self.preferred_locale = payload.get("preferred_locale")
        self.vanity_url_code = payload.get("vanity_url_code")
        self.verification_level = payload.get("verification_level")
        self.premium_tier = payload.get("premium_tier", 0)
        self.joined_at = parse_timestamp(payload.get("joined_at"))
        self.large = payload.get("large", False)
        self.unavailable = payload.get("unavailable", False)

    @property
    def icon_url(self) -> str | None:
        return _icon_url("icons", self._id, self.icon)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Guild {self.name} ({self._id})>"


def _icon_url(path: str, entity_id: str, icon: str | None, animated: bool = False) -> str | None:
    if not icon:
        return None
    ext = "gif" if animated or icon.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/{path}/{entity_id}/{icon}.{ext}"


class Invite(Model):
    __slots__ = ("code", "guild", "channel", "inviter", "target_user", "target_type",
                 "approximate_member_count", "approximate_presence_count",
                 "expires_at", "uses", "max_uses", "max_age", "temporary")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.code = payload.get("code") or self._id
        self.guild = _nested(payload, "guild", Guild)
        self.channel = _nested(payload, "channel", Channel)
        self.inviter = _nested(payload, "inviter", User)
        self.target_user = _nested(payload, "target_user", User)
        self.target_type = payload.get("target_type")
        self.approximate_member_count = payload.get("approximate_member_count")
        self.approximate_presence_count = payload.get("approximate_presence_count")
        self.expires_at = parse_timestamp(payload.get("expires_at"))
        self.uses = payload.get("uses")
        self.max_uses = payload.get("max_uses")
        self.max_age = payload.get("max_age")
        self.temporary = payload.get("temporary", False)

    @property
    def url(self) -> str:
        return f"https://discord.gg/{self.code}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Invite {self.code}>"


class VoiceState(Model):
    __slots__ = ("guild_id", "channel_id", "user_id", "session_id", "deaf", "mute",
                 "self_deaf", "self_mute", "self_stream", "self_video", "suppress",
                 "request_to_speak_timestamp")

    def __init__(self, data: dict | None) -> None:
        super().__init__(data)
        payload = self._raw
        self.guild_id = str(payload.get("guild_id") or "")
        self.channel_id = str(payload.get("channel_id") or "")
        self.user_id = str(payload.get("user_id") or "")
        if not self._id:
            self._id = self.user_id
        self.session_id = payload.get("session_id")
        self.deaf = payload.get("deaf", False)
        self.mute = payload.get("mute", False)
        self.self_deaf = payload.get("self_deaf", False)
        self.self_mute = payload.get("self_mute", False)
        self.self_stream = payload.get("self_stream", False)
        self.self_video = payload.get("self_video", False)
        self.suppress = payload.get("suppress", False)
        self.request_to_speak_timestamp = parse_timestamp(
            payload.get("request_to_speak_timestamp")
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<VoiceState {self.user_id} in {self.channel_id}>"


__all__ = [
    "Model",
    "User",
    "Member",
    "Relationship",
    "Role",
    "Emoji",
    "Sticker",
    "Attachment",
    "Embed",
    "EmbedField",
    "Reaction",
    "MessageReference",
    "Message",
    "Channel",
    "Guild",
    "Invite",
    "VoiceState",
    "parse_timestamp",
    "clean_content",
    "MESSAGE_TYPES",
    "MESSAGE_TYPES_COUNTED",
    "MESSAGE_FLAG_SUPPRESS_EMBEDS",
    "MESSAGE_FLAG_COMPONENTS_V2",
    "MESSAGE_FLAG_EPHEMERAL",
    "MESSAGE_FLAG_IS_VOICE_MESSAGE",
]


# Public alias so `from despise.models import clean_content` works.
clean_content = _clean_content
