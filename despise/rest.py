"""Tiered REST surface. All calls borrow the shared DespiseHttp.

T0 core: me, gateway
T1 DMs/messaging: open DM, send/edit/delete, typing, pins, history
T2 servers: invite accept, fetch by id, leave, gateway guild-subscribe
T3 presence: op3 rich-presence builder (throttle lives on Gateway)
T4 relationships: list/add/remove/block
T5 interactions: slash command + button/select/modal callbacks
T6 platform: webhooks, newsletters, follows

OUT (v1): voice media/RTC, protobuf settings, store/billing,
          ext.commands, full member cache.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime
from urllib.parse import quote

from .gateway import PRESENCE_MIN_INTERVAL_S
from .http import DespiseHttp
from .models import (
    Channel,
    Emoji,
    Guild,
    Invite,
    Member,
    Message,
    Relationship,
    Role,
    User,
)

log = logging.getLogger("despise.rest")

# activity types: 0 game, 1 streaming, 2 listening, 3 watching, 4 custom, 5 competing
ACTIVITY_TYPES = {
    "game": 0,
    "streaming": 1,
    "listening": 2,
    "watching": 3,
    "custom": 4,
    "competing": 5,
}

# Re-exported for callers that tuned around the old module-level throttle;
# the value itself is enforced on Gateway.
__all__ = [
    "ACTIVITY_TYPES",
    "PRESENCE_MIN_INTERVAL_S",
    "TYPING_REFRESH_S",
    "File",
    "Rest",
    "TypingLoop",
    "build_activity",
    "clear_presence",
    "set_presence",
]

# Discord drops a typing indicator after ~10s of silence, so the refresh loop
# must run faster than that or the user stops appearing to type.
TYPING_REFRESH_S = 8.0


class TypingLoop:
    """Keeps a typing indicator alive in a channel while in use.

    Use as an async context manager, or call ``start()`` / ``stop()``::

        async with rest.typing_loop(cid):
            await asyncio.sleep(30)   # still typing the whole time
    """

    __slots__ = ("_http", "channel_id", "interval", "_task", "_stopping")

    def __init__(
        self,
        http: DespiseHttp,
        channel_id: str,
        *,
        interval: float = TYPING_REFRESH_S,
    ) -> None:
        if interval <= 0:
            raise ValueError("typing interval must be positive")
        self._http = http
        self.channel_id = channel_id
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def _run(self) -> None:
        try:
            while not self._stopping:
                try:
                    await self._http.request("POST", f"/channels/{self.channel_id}/typing")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A failed typing POST is cosmetic; keep the loop alive.
                    log.debug("typing indicator failed: %r", exc)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> TypingLoop:
        """Send immediately, then refresh on the interval. Idempotent."""
        if self._task is not None and not self._task.done():
            self._stopping = False
            return self
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name=f"despise-typing-{self.channel_id}"
        )
        return self

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def __aenter__(self) -> TypingLoop:
        return self.start()

    async def __aexit__(self, *exc) -> None:
        self.stop()


def build_activity(
    name: str,
    *,
    kind: str = "game",
    details: str | None = None,
    state: str | None = None,
    application_id: str | None = None,
    assets: dict | None = None,
    buttons: list | None = None,
    url: str | None = None,
) -> dict:
    act: dict = {"name": name, "type": ACTIVITY_TYPES[kind]}
    if details is not None:
        act["details"] = details
    if state is not None:
        act["state"] = state
    if application_id is not None:
        act["application_id"] = application_id
    if assets is not None:
        act["assets"] = assets
    if buttons is not None:
        act["buttons"] = buttons
    if url is not None:
        act["url"] = url
    return act


class File:
    """An attachment to upload with a message.

    ``data`` holds bytes; ``local_path`` streams from disk instead. Prefer
    :meth:`from_path` for files on disk::

        from despise import File

        await bot.send(cid, "look", files=[File.from_path("cat.png")])
        await bot.send(cid, files=File(b"...", filename="blob.bin"))

    A plain ``str``/``PathLike`` path, a ``bytes`` blob, a
    ``(filename, data[, content_type])`` tuple, or a dict are all accepted
    wherever a file is, and are normalised to ``File``.
    """

    __slots__ = ("filename", "data", "local_path", "content_type")

    def __init__(
        self,
        data: bytes | None = None,
        *,
        filename: str | None = None,
        local_path: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        if data is None and local_path is None:
            raise ValueError("File needs data or a path")
        if data is not None and local_path is not None:
            raise ValueError("File takes data or a path, not both")
        if filename is None:
            filename = os.path.basename(local_path) if local_path else "file"
        self.data = data
        self.local_path = local_path
        self.filename = filename
        self.content_type = content_type

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike,
        *,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> File:
        return cls(
            None,
            filename=filename or os.path.basename(str(path)),
            local_path=str(path),
            content_type=content_type,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<File {self.filename}>"


def _resolve_files(files) -> list[File]:
    """Normalise whatever the caller passed into a list of ``File``."""
    if files is None or files == []:
        return []
    if isinstance(files, File | bytes | tuple | dict | str | os.PathLike):
        files = [files]
    out: list[File] = []
    for item in files:
        if isinstance(item, File):
            out.append(item)
        elif isinstance(item, bytes):
            out.append(File(item, filename="file.bin"))
        elif isinstance(item, str | os.PathLike):
            out.append(File.from_path(item))
        elif isinstance(item, tuple):
            name, data = item[0], item[1]
            content_type = item[2] if len(item) > 2 else "application/octet-stream"
            out.append(File(data, filename=name, content_type=content_type))
        elif isinstance(item, dict):
            out.append(File(**item))
        else:
            raise TypeError(f"cannot use {type(item).__name__} as a file")
    return out


def _isoformat(value) -> str:
    """Render a datetime (or an already-formatted string) as ISO8601."""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _normalise_embeds(embeds):
    """Turn model embeds into JSON-ready dicts (plain dicts pass through)."""
    out = []
    for embed in embeds:
        to_dict = getattr(embed, "to_dict", None)
        out.append(to_dict() if callable(to_dict) else embed)
    return out


def _normalise_response(response: dict) -> dict:
    """Copy an interaction response with model embeds converted to dicts."""
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("embeds"), list):
        return {**response, "data": {**data, "embeds": _normalise_embeds(data["embeds"])}}
    return response


def _emoji_param(emoji) -> str:
    """Turn an emoji into the path segment a reaction route expects.

    Accepts a :class:`~despise.models.Emoji`, a custom-emoji mention string
    (``<:name:id>`` / ``<a:name:id>``), a ``name:id`` pair, or a unicode
    emoji. The result is unquoted; callers quote it when building the URL.
    """
    if isinstance(emoji, Emoji):
        return f"{emoji.name}:{emoji._id}" if emoji._id else emoji.name
    name = str(emoji)
    if name.startswith("<") and name.endswith(">"):
        parts = name.strip("<>").split(":")
        if len(parts) == 3:  # a:name:id or :name:id
            return f"{parts[1]}:{parts[2]}"
    return name


def _custom_status_emoji(emoji) -> tuple[str | None, str | None]:
    """Split a custom-status emoji into Discord's mutually exclusive fields.

    Custom emoji (an :class:`~despise.models.Emoji`, ``<:name:id>`` mention,
    or ``name:id`` pair) become ``emoji_id``. A unicode emoji becomes
    ``emoji_name``.
    """
    if emoji is None:
        return None, None
    if isinstance(emoji, Emoji):
        return (emoji._id, None) if emoji._id else (None, emoji.name)

    value = str(emoji)
    if value.startswith("<") and value.endswith(">"):
        parts = value.strip("<>").split(":")
        if len(parts) == 3 and parts[2]:
            return parts[2], None
    if ":" in value:
        _, _, emoji_id = value.rpartition(":")
        if emoji_id:
            return emoji_id, None
    return None, value


class Rest:
    """Borrowed-http REST helpers. Stateless; Client owns one.

    ``client`` is optional and only used to bind returned models back to a
    Client, so ``msg.reply(...)`` works on anything this object hands out.
    """

    def __init__(self, http: DespiseHttp, *, client=None) -> None:
        self.http = http
        self._client = client

    def _bind(self, model):
        """Attach the owning Client to a model, when there is one."""
        if self._client is not None and model is not None:
            model._bind(self._client)
        return model

    # -- T0 -----------------------------------------------------------
    async def me(self) -> User:
        data = await self.http.request("GET", "/users/@me")
        assert isinstance(data, dict)
        return self._bind(User(data))

    # -- T1 -----------------------------------------------------------
    async def open_dm(self, user_id: str) -> Channel:
        data = await self.http.request(
            "POST", "/users/@me/channels", json_data={"recipient_id": user_id}
        )
        assert isinstance(data, dict)
        return self._bind(Channel(data))

    async def create_group_dm(
        self, recipient_ids, *, nicks: dict | None = None
    ) -> Channel:
        """Create (or fetch) a group DM for 1+ other users.

        ``recipient_ids`` are *user* ids. ``nicks`` maps user id -> the
        nickname to show inside the group. A single recipient just opens a DM.
        """
        payload: dict = {"recipients": [str(uid) for uid in recipient_ids]}
        if nicks:
            payload["nicks"] = {str(k): v for k, v in nicks.items()}
        data = await self.http.request(
            "POST", "/users/@me/channels", json_data=payload
        )
        assert isinstance(data, dict)
        return self._bind(Channel(data))

    async def private_channels(self) -> list[Channel]:
        """Every DM and group DM for the current user."""
        data = await self.http.request("GET", "/users/@me/channels")
        assert isinstance(data, list)
        return [self._bind(Channel(c)) for c in data]

    async def get_channel(self, channel_id: str) -> Channel:
        data = await self.http.request("GET", f"/channels/{channel_id}")
        assert isinstance(data, dict)
        return self._bind(Channel(data))

    async def edit_channel(
        self, channel_id: str, *, reason: str | None = None, **fields
    ) -> Channel:
        """PATCH a channel. ``fields`` are Discord's modify-channel params.

        ``name``, ``topic``, ``nsfw``, ``position``, ``rate_limit_per_user``,
        ``parent_id``, ``bitrate``/``user_limit`` (voice), ``archived`` /
        ``locked`` / ``auto_archive_duration`` (threads), ``flags``, ``type``...
        pass them straight through to the API.
        """
        data = await self.http.request(
            "PATCH", f"/channels/{channel_id}", json_data=fields, reason=reason
        )
        assert isinstance(data, dict)
        return self._bind(Channel(data))

    async def delete_channel(
        self, channel_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE", f"/channels/{channel_id}", reason=reason
        )

    async def edit_channel_permissions(
        self,
        channel_id: str,
        overwrite_id: str,
        *,
        allow: str | int = "0",
        deny: str | int = "0",
        type: int = 0,
        reason: str | None = None,
    ) -> None:
        """Create or replace a channel permission overwrite.

        ``type`` is ``0`` for a role overwrite or ``1`` for a member one;
        ``allow``/``deny`` are permission bitfields (ints are stringified).
        """
        await self.http.request(
            "PUT",
            f"/channels/{channel_id}/permissions/{overwrite_id}",
            json_data={"type": type, "allow": str(allow), "deny": str(deny)},
            reason=reason,
        )

    async def delete_channel_permission(
        self, channel_id: str, overwrite_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE",
            f"/channels/{channel_id}/permissions/{overwrite_id}",
            reason=reason,
        )

    async def add_recipient(self, channel_id: str, user_id: str):
        """Add a user to a group DM.

        Returns the resulting :class:`Channel` when Discord converts a DM into
        a group DM, else ``None`` (204).
        """
        data = await self.http.request(
            "PUT", f"/channels/{channel_id}/recipients/{user_id}"
        )
        return self._bind(Channel(data)) if isinstance(data, dict) else None

    async def remove_recipient(self, channel_id: str, user_id: str) -> None:
        await self.http.request(
            "DELETE", f"/channels/{channel_id}/recipients/{user_id}"
        )

    async def guild_channels(self, guild_id: str) -> list[Channel]:
        data = await self.http.request("GET", f"/guilds/{guild_id}/channels")
        assert isinstance(data, list)
        return [self._bind(Channel(c)) for c in data]

    async def send_message(
        self,
        channel_id: str,
        content: str | None = None,
        *,
        tts: bool = False,
        embeds: list | None = None,
        components: list | None = None,
        files=None,
        poll: dict | None = None,
        stickers: list | None = None,
        nonce: str | None = None,
        reply_to: str | None = None,
        allowed_mentions: dict | None = None,
    ) -> Message:
        """Send one message to a channel.

        The single send path: text, embeds, component rows, a poll, stickers,
        and file attachments all route through here. Files switch the request
        to multipart automatically (Discord requires it) and every other field
        rides along in ``payload_json``.
        """
        payload: dict = {"tts": tts}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = _normalise_embeds(embeds)
        if components is not None:
            payload["components"] = components
        if poll is not None:
            payload["poll"] = poll
        if stickers is not None:
            payload["sticker_ids"] = [str(s) for s in stickers]
        if nonce is not None:
            payload["nonce"] = nonce
        if reply_to is not None:
            payload["message_reference"] = {"message_id": str(reply_to)}
        if allowed_mentions is not None:
            payload["allowed_mentions"] = allowed_mentions

        path = f"/channels/{channel_id}/messages"
        resolved = _resolve_files(files)
        if resolved:
            payload["attachments"] = [
                {"id": index, "filename": item.filename}
                for index, item in enumerate(resolved)
            ]
            data = await self.http.upload(
                path,
                fields={"payload_json": json.dumps(payload)},
                files=[
                    {
                        "field": f"files[{index}]",
                        "filename": item.filename,
                        "content_type": item.content_type,
                        "data": item.data,
                        "local_path": item.local_path,
                    }
                    for index, item in enumerate(resolved)
                ],
            )
        else:
            data = await self.http.request("POST", path, json_data=payload)
        assert isinstance(data, dict)
        return self._bind(Message(data))

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str | None = None,
        *,
        embeds: list | None = None,
        components: list | None = None,
        allowed_mentions: dict | None = None,
    ) -> Message:
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = _normalise_embeds(embeds)
        if components is not None:
            payload["components"] = components
        if allowed_mentions is not None:
            payload["allowed_mentions"] = allowed_mentions
        data = await self.http.request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json_data=payload,
        )
        assert isinstance(data, dict)
        return self._bind(Message(data))

    async def fetch_message(self, channel_id: str, message_id: str) -> Message:
        data = await self.http.request(
            "GET", f"/channels/{channel_id}/messages/{message_id}"
        )
        assert isinstance(data, dict)
        return self._bind(Message(data))

    async def delete_message(
        self, channel_id: str, message_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}",
            reason=reason,
        )

    # -- reactions --------------------------------------------------------
    async def add_reaction(
        self, channel_id: str, message_id: str, emoji
    ) -> None:
        param = quote(_emoji_param(emoji), safe="")
        await self.http.request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{param}/@me",
        )

    async def remove_own_reaction(
        self, channel_id: str, message_id: str, emoji
    ) -> None:
        param = quote(_emoji_param(emoji), safe="")
        await self.http.request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{param}/@me",
        )

    async def remove_reaction(
        self, channel_id: str, message_id: str, emoji, user_id: str
    ) -> None:
        param = quote(_emoji_param(emoji), safe="")
        await self.http.request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}/reactions/"
            f"{param}/{user_id}",
        )

    async def clear_reactions(
        self, channel_id: str, message_id: str, *, emoji=None
    ) -> None:
        """Remove every reaction, or every reaction for one emoji."""
        path = f"/channels/{channel_id}/messages/{message_id}/reactions"
        if emoji is not None:
            path += f"/{quote(_emoji_param(emoji), safe='')}"
        await self.http.request("DELETE", path)

    async def typing(self, channel_id: str) -> None:
        await self.http.request("POST", f"/channels/{channel_id}/typing")

    def typing_loop(
        self, channel_id: str, *, interval: float = TYPING_REFRESH_S
    ) -> TypingLoop:
        """A long-running typing indicator.

        Discord expires the indicator roughly 10s after the last POST, so the
        loop refreshes every :data:`TYPING_REFRESH_S` (8s) by default. The
        one-shot ``typing()`` above expires almost immediately::

            async with bot.rest.typing_loop(cid):
                await bot.rest.send_message(cid, "thinking about it...")
        """
        return TypingLoop(self.http, channel_id, interval=interval)

    async def history(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
        around: str | None = None,
    ) -> list[Message]:
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if before is not None:
            params.append(("before", before))
        if after is not None:
            params.append(("after", after))
        if around is not None:
            params.append(("around", around))
        data = await self.http.request(
            "GET", f"/channels/{channel_id}/messages", params=params
        )
        assert isinstance(data, list)
        return [self._bind(Message(m)) for m in data]

    async def pinned_messages(
        self, channel_id: str, *, limit: int = 50, before=None
    ) -> list[Message]:
        """List a channel's pins (``GET .../messages/pins``).

        Each response item is ``{"pinned_at": ..., "message": {...}}``; the
        returned messages are the unwrapped ``message`` objects. ``before`` is
        an ISO8601 timestamp (a ``datetime`` is converted automatically), not
        a message snowflake.
        """
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if before is not None:
            params.append(("before", _isoformat(before)))
        data = await self.http.request(
            "GET", f"/channels/{channel_id}/messages/pins", params=params
        )
        items = data.get("items", []) if isinstance(data, dict) else data
        assert isinstance(items, list)
        messages = []
        for item in items:
            if not isinstance(item, dict):
                continue
            payload = item.get("message", item)
            if isinstance(payload, dict):
                messages.append(self._bind(Message(payload)))
        return messages

    async def pin(self, channel_id: str, message_id: str) -> None:
        # Discord documents pins under ``/messages/pins``; the bare ``/pins``
        # path this used to hit is not part of the API.
        await self.http.request(
            "PUT", f"/channels/{channel_id}/messages/pins/{message_id}"
        )

    async def unpin(self, channel_id: str, message_id: str) -> None:
        await self.http.request(
            "DELETE", f"/channels/{channel_id}/messages/pins/{message_id}"
        )

    async def crosspost_message(self, channel_id: str, message_id: str) -> Message:
        """Publish a message from an announcement channel to followers."""
        data = await self.http.request(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/crosspost",
        )
        assert isinstance(data, dict)
        return self._bind(Message(data))

    # -- read state ------------------------------------------------------
    # Docs: https://docs.discord.food/topics/read-state
    async def ack_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        token: str | None = None,
        manual: bool | None = None,
        mention_count: int | None = None,
        flags: int | None = None,
        last_viewed: int | None = None,
    ) -> None:
        """Mark a channel read up to ``message_id`` (fires Message Ack).

        ``message_id`` only has to be a valid snowflake. When moving the
        marker *backwards*, pass ``manual=True`` and the real
        ``mention_count``, otherwise clients may ignore the update.
        ``last_viewed`` is days since the Discord epoch.
        """
        payload: dict = {"token": token}
        for key, value in (
            ("manual", manual),
            ("mention_count", mention_count),
            ("flags", flags),
            ("last_viewed", last_viewed),
        ):
            if value is not None:
                payload[key] = value
        await self.http.request(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/ack",
            json_data=payload,
        )

    async def ack_pins(self, channel_id: str) -> None:
        """Acknowledge the current pinned messages (fires Channel Pins Ack)."""
        await self.http.request("POST", f"/channels/{channel_id}/pins/ack")

    async def ack_guild(self, guild_id: str) -> None:
        """Acknowledge every read state in a guild. Deprecated by Discord."""
        await self.http.request("POST", f"/guilds/{guild_id}/ack")

    async def ack_guild_feature(
        self, guild_id: str, read_state_type: int, entity_id: str
    ) -> None:
        """Ack a guild feature read state (scheduled event, onboarding, ...)."""
        await self.http.request(
            "POST", f"/guilds/{guild_id}/ack/{read_state_type}/{entity_id}"
        )

    async def ack_user_feature(self, read_state_type: int, entity_id: str) -> None:
        """Ack a user-level read state (guild home, message requests, ...)."""
        await self.http.request(
            "POST", f"/users/@me/{read_state_type}/{entity_id}/ack"
        )

    async def ack_bulk(self, read_states: list[dict]) -> None:
        """Update many read states in one request.

        Each entry is ``{"channel_id": ..., "message_id": ...}`` with an
        optional ``read_state_type`` (default ``0``, CHANNEL). Unlike the
        single ack, ``message_id`` must be greater than ``0``.
        """
        await self.http.request(
            "POST", "/read-states/ack-bulk", json_data={"read_states": read_states}
        )

    async def delete_read_state(
        self,
        read_state_id: str,
        *,
        read_state_type: int | None = None,
        version: int | None = None,
    ) -> None:
        """Delete a read state by its resource id.

        ``read_state_type`` selects the kind of resource (default CHANNEL);
        pass it when deleting a guild/feature read state.
        """
        payload: dict = {}
        if read_state_type is not None:
            payload["read_state_type"] = read_state_type
        if version is not None:
            payload["version"] = version
        await self.http.request(
            "DELETE",
            f"/channels/{read_state_id}/messages/ack",
            json_data=payload or None,
        )

    # -- T2 -----------------------------------------------------------
    async def accept_invite(self, code: str) -> dict:
        data = await self.http.request("POST", f"/invites/{code}", context="accept")
        assert isinstance(data, dict)
        return data

    async def get_guild(self, guild_id: str) -> Guild:
        data = await self.http.request("GET", f"/guilds/{guild_id}")
        assert isinstance(data, dict)
        return self._bind(Guild(data))

    async def leave_guild(self, guild_id: str) -> None:
        await self.http.request("DELETE", f"/users/@me/guilds/{guild_id}")

    async def guild_preview(self, guild_id: str) -> Guild:
        """Partial guild object (works for discoverable guilds we are not in)."""
        data = await self.http.request("GET", f"/guilds/{guild_id}/preview")
        assert isinstance(data, dict)
        return self._bind(Guild(data))

    async def get_member(self, guild_id: str, user_id: str) -> Member:
        data = await self.http.request(
            "GET", f"/guilds/{guild_id}/members/{user_id}"
        )
        assert isinstance(data, dict)
        # The member object carries no guild id of its own; stamp it so
        # ``member.guild_id`` and ``client.get_member`` line up.
        return self._bind(Member({**data, "guild_id": str(guild_id)}))

    async def edit_member(
        self,
        guild_id: str,
        user_id: str,
        *,
        nick: str | None = None,
        roles: list[str] | None = None,
        mute: bool | None = None,
        deaf: bool | None = None,
        channel_id: str | None = None,
        communication_disabled_until: str | None = None,
        reason: str | None = None,
    ) -> Member:
        """Modify a guild member (nick, roles, voice state, timeout)."""
        payload: dict = {}
        for key, value in (
            ("nick", nick),
            ("roles", [str(r) for r in roles] if roles is not None else None),
            ("mute", mute),
            ("deaf", deaf),
            ("channel_id", channel_id),
            ("communication_disabled_until", communication_disabled_until),
        ):
            if value is not None:
                payload[key] = value
        data = await self.http.request(
            "PATCH",
            f"/guilds/{guild_id}/members/{user_id}",
            json_data=payload,
            reason=reason,
        )
        assert isinstance(data, dict)
        return self._bind(Member({**data, "guild_id": str(guild_id)}))

    async def kick_member(
        self, guild_id: str, user_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason
        )

    async def add_member_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self.http.request(
            "PUT",
            f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            reason=reason,
        )

    async def remove_member_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self.http.request(
            "DELETE",
            f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            reason=reason,
        )

    async def bans(
        self,
        guild_id: str,
        *,
        limit: int = 1000,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict]:
        """List guild bans. User accounts must pass an explicit ``limit``."""
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if before is not None:
            params.append(("before", str(before)))
        if after is not None:
            params.append(("after", str(after)))
        data = await self.http.request(
            "GET", f"/guilds/{guild_id}/bans", params=params
        )
        assert isinstance(data, list)
        return data

    async def ban(
        self,
        guild_id: str,
        user_id: str,
        *,
        delete_message_seconds: int = 0,
        reason: str | None = None,
    ) -> None:
        await self.http.request(
            "PUT",
            f"/guilds/{guild_id}/bans/{user_id}",
            json_data={"delete_message_seconds": delete_message_seconds},
            reason=reason,
        )

    async def unban(
        self, guild_id: str, user_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE", f"/guilds/{guild_id}/bans/{user_id}", reason=reason
        )

    async def create_role(
        self,
        guild_id: str,
        *,
        name: str | None = None,
        permissions: str | int | None = None,
        color: int | None = None,
        hoist: bool | None = None,
        mentionable: bool | None = None,
        unicode_emoji: str | None = None,
        reason: str | None = None,
        **fields,
    ) -> Role:
        """Create a role. ``permissions`` may be an int (stringified for you)."""
        payload: dict = dict(fields)
        for key, value in (
            ("name", name),
            ("permissions", str(permissions) if permissions is not None else None),
            ("color", color),
            ("hoist", hoist),
            ("mentionable", mentionable),
            ("unicode_emoji", unicode_emoji),
        ):
            if value is not None:
                payload[key] = value
        data = await self.http.request(
            "POST", f"/guilds/{guild_id}/roles", json_data=payload, reason=reason
        )
        assert isinstance(data, dict)
        return self._bind(Role(data))

    async def edit_role(
        self,
        guild_id: str,
        role_id: str,
        *,
        reason: str | None = None,
        **fields,
    ) -> Role:
        """PATCH a role; ``fields`` are passed through (``name``, ``color``,
        ``hoist``, ``mentionable``, ``permissions``, ``position``, ...)."""
        data = await self.http.request(
            "PATCH",
            f"/guilds/{guild_id}/roles/{role_id}",
            json_data=fields,
            reason=reason,
        )
        assert isinstance(data, dict)
        return self._bind(Role(data))

    async def delete_role(
        self, guild_id: str, role_id: str, *, reason: str | None = None
    ) -> None:
        await self.http.request(
            "DELETE", f"/guilds/{guild_id}/roles/{role_id}", reason=reason
        )

    async def subscribe_guild(
        self,
        gateway,
        guild_id: str,
        *,
        channels: dict | None = None,
    ) -> None:
        """Guild channel-subscribe via gateway op 14 (no member cache kept).

        Thin wrapper kept for the pre-Client API; prefer
        ``client.gateway.subscribe_guild(...)``.
        """
        await gateway.subscribe_guild(guild_id, channels=channels)

    # -- T4 -----------------------------------------------------------
    async def relationships(self) -> list[Relationship]:
        data = await self.http.request("GET", "/users/@me/relationships")
        assert isinstance(data, list)
        return [Relationship(r) for r in data]

    async def add_friend(self, user_id: str, *, note: str | None = None) -> None:
        """Send a friend request.

        Discord returns ``204`` with no body, so there is no relationship
        object to hand back; listen for ``RELATIONSHIP_ADD`` for that.
        """
        payload: dict = {}
        if note is not None:
            payload["note"] = note
        await self.http.request(
            "PUT",
            f"/users/@me/relationships/{user_id}",
            json_data=payload,
            context="friends",
        )

    async def remove_relationship(self, user_id: str) -> None:
        await self.http.request("DELETE", f"/users/@me/relationships/{user_id}")

    async def block(self, user_id: str) -> None:
        """Block a user (relationship type 2). Returns nothing (204)."""
        await self.http.request(
            "PUT", f"/users/@me/relationships/{user_id}", json_data={"type": 2}
        )

    # -- users -----------------------------------------------------------
    async def get_user(self, user_id: str) -> User:
        data = await self.http.request("GET", f"/users/{user_id}")
        assert isinstance(data, dict)
        return self._bind(User(data))

    async def user_profile(
        self,
        user_id: str,
        *,
        guild_id: str | None = None,
        with_mutual_guilds: bool | None = None,
        with_mutual_friends: bool | None = None,
    ) -> dict:
        """Fetch a user profile (mutual guilds/friends, bio, connected accounts).

        Requires a mutual guild, a friend relationship, or a ``guild_id`` we
        share with them.
        """
        params: list[tuple[str, str]] = []
        if guild_id is not None:
            params.append(("guild_id", str(guild_id)))
        if with_mutual_guilds is not None:
            params.append(("with_mutual_guilds", str(with_mutual_guilds).lower()))
        if with_mutual_friends is not None:
            params.append(("with_mutual_friends", str(with_mutual_friends).lower()))
        data = await self.http.request(
            "GET", f"/users/{user_id}/profile", params=params or None
        )
        assert isinstance(data, dict)
        return data

    async def get_note(self, user_id: str) -> str:
        """The current user's private note on a user (``""`` when unset)."""
        data = await self.http.request("GET", f"/users/@me/notes/{user_id}")
        assert isinstance(data, dict)
        return data.get("note") or ""

    async def set_note(self, user_id: str, note: str | None) -> None:
        """Set (or clear, with ``None``) the private note on a user."""
        await self.http.request(
            "PUT", f"/users/@me/notes/{user_id}", json_data={"note": note}
        )

    # -- profile and settings -------------------------------------------
    async def edit_profile(self, **fields) -> dict:
        """PATCH the current user's profile.

        Supported fields: ``bio``, ``pronouns``, ``banner``, ``accent_color``,
        ``theme_colors``, ``profile_effect_id`` (plus deprecated Discord
        fields). Pass ``None`` for a nullable field to clear it. Use
        :meth:`set_banner` when uploading banner bytes.
        """
        data = await self.http.request(
            "PATCH", "/users/@me/profile", json_data=fields
        )
        assert isinstance(data, dict)
        return data

    async def set_bio(self, bio: str | None) -> dict:
        """Set the global profile bio. ``None`` clears it."""
        return await self.edit_profile(bio=bio)

    async def edit_user(self, **fields) -> dict:
        """PATCH the current user account.

        Supports ``avatar_decoration_sku_id``, ``display_name_font_id``,
        ``display_name_effect_id``, ``display_name_colors``, ``bio``,
        ``pronouns``, ``banner``, ``accent_color``, ``global_name``, and other
        fields from Discord's Modify Current User endpoint. Pass ``None`` to
        clear a nullable field.
        """
        data = await self.http.request("PATCH", "/users/@me", json_data=fields)
        assert isinstance(data, dict)
        return data

    async def set_avatar_decoration(self, sku_id: str | None) -> dict:
        """Equip an avatar decoration by SKU ID. ``None`` removes it."""
        return await self.edit_user(avatar_decoration_sku_id=sku_id)

    async def set_display_name_style(
        self,
        *,
        font_id: int | None = None,
        effect_id: int | None = None,
        colors: list[int] | None = None,
    ) -> dict:
        """Set the display-name font, effect, and colors.

        Omitted arguments are left unchanged. Pass all three to replace the
        whole style; use :meth:`edit_user` with explicit ``None`` values to
        clear individual fields. ``colors`` are hex integers.
        """
        payload: dict = {}
        if font_id is not None:
            payload["display_name_font_id"] = font_id
        if effect_id is not None:
            payload["display_name_effect_id"] = effect_id
        if colors is not None:
            payload["display_name_colors"] = colors
        return await self.edit_user(**payload)

    async def edit_guild_profile(self, guild_id: str, **fields) -> dict:
        """PATCH the current user's profile in one guild.

        Supported fields: ``pronouns``, ``bio``, ``banner``, ``accent_color``,
        ``theme_colors``, ``profile_effect_id``. Pass ``None`` to clear a
        nullable field.
        """
        data = await self.http.request(
            "PATCH", f"/guilds/{guild_id}/profile/@me", json_data=fields
        )
        assert isinstance(data, dict)
        return data

    async def set_guild_bio(self, guild_id: str, bio: str | None) -> dict:
        """Set the guild-specific bio. ``None`` clears it."""
        return await self.edit_guild_profile(guild_id, bio=bio)

    async def set_guild_nickname(
        self,
        guild_id: str,
        nick: str | None,
        *,
        reason: str | None = None,
    ) -> Member:
        """Set the current user's nickname in one guild. ``None`` clears it."""
        data = await self.http.request(
            "PATCH",
            f"/guilds/{guild_id}/members/@me",
            json_data={"nick": nick},
            reason=reason,
        )
        assert isinstance(data, dict)
        return self._bind(Member({**data, "guild_id": str(guild_id)}))

    async def set_custom_status(
        self,
        text: str | None = None,
        *,
        emoji: str | Emoji | None = None,
        expires_at: datetime | str | None = None,
    ) -> dict:
        """Replace the custom status shown in the profile popout.

        ``emoji`` accepts an :class:`~despise.models.Emoji`, a
        ``<:name:id>`` / ``name:id`` custom emoji, or a unicode emoji.
        Unspecified emoji and expiry are cleared, matching Discord's "replace
        the status" behavior. ``set_custom_status(None)`` clears it.
        """
        emoji_id, emoji_name = _custom_status_emoji(emoji)
        custom_status = {
            "text": text,
            "emoji_id": emoji_id,
            "emoji_name": emoji_name,
            "expires_at": _isoformat(expires_at) if expires_at is not None else None,
        }
        data = await self.http.request(
            "PATCH",
            "/users/@me/settings",
            json_data={"custom_status": custom_status},
        )
        assert isinstance(data, dict)
        return data

    # -- T5: interactions ---------------------------------------------
    async def run_interaction(self, payload: dict) -> None:
        """Send an interaction (``POST /interactions``).

        ``payload`` is the interaction object as Discord sent it, plus the
        ``session_id`` needed to actually execute a command. For a slash
        command::

            await rest.run_interaction({
                **interaction,          # t=2/4, id, application_id, data, ...
                "session_id": session_id,
            })

        When the Rest belongs to a connected Client, the gateway session id
        is filled in automatically.
        """
        payload = dict(payload)
        if "session_id" not in payload and self._client is not None:
            session_id = getattr(self._client.gateway, "session_id", None)
            if session_id:
                payload["session_id"] = session_id
        await self.http.request("POST", "/interactions", json_data=payload)

    async def interaction_callback(
        self,
        interaction_id: str,
        token: str,
        response: dict,
        *,
        files=None,
    ) -> None:
        """Answer an interaction. ``response`` comes from ``interactions.py``.

        Pass ``files=`` for attachment uploads; they are sent as multipart
        with attachment metadata added to the response automatically.
        """
        response = _normalise_response(response)
        path = f"/interactions/{interaction_id}/{token}/callback"
        resolved = _resolve_files(files)
        if not resolved:
            await self.http.request("PUT", path, json_data=response)
            return
        data = dict(response.get("data") or {})
        data["attachments"] = [
            {"id": index, "filename": item.filename}
            for index, item in enumerate(resolved)
        ]
        response = {**response, "data": data}
        await self.http.upload(
            path,
            method="PUT",
            fields={"payload_json": json.dumps(response)},
            files=[
                {
                    "field": f"files[{index}]",
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "data": item.data,
                    "local_path": item.local_path,
                }
                for index, item in enumerate(resolved)
            ],
        )

    async def edit_interaction_response(
        self,
        application_id: str,
        token: str,
        *,
        content: str | None = None,
        embeds: list | None = None,
        components: list | None = None,
    ) -> Message:
        """PATCH the original interaction response."""
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = _normalise_embeds(embeds)
        if components is not None:
            payload["components"] = components
        data = await self.http.request(
            "PATCH",
            f"/webhooks/{application_id}/{token}/messages/@original",
            json_data=payload,
        )
        assert isinstance(data, dict)
        return self._bind(Message(data))

    async def original_response(
        self, application_id: str, token: str
    ) -> Message:
        """GET the message an interaction created."""
        data = await self.http.request(
            "GET",
            f"/webhooks/{application_id}/{token}/messages/@original",
        )
        assert isinstance(data, dict)
        return self._bind(Message(data))

    # -- T6: platform -------------------------------------------------
    async def get_invite(self, code: str) -> Invite:
        data = await self.http.request(
            "GET", f"/invites/{code}", params={"with_counts": "true"}
        )
        assert isinstance(data, dict)
        return Invite(data)

    async def follow_channel(
        self, channel_id: str, *, target_channel_id: str
    ) -> dict:
        """Follow a News channel into ``target_channel_id``.

        Returns the followed-channel object (``{"channel_id", "webhook_id"}``),
        not a :class:`Channel`.
        """
        data = await self.http.request(
            "POST",
            f"/channels/{channel_id}/followers",
            json_data={"webhook_channel_id": str(target_channel_id)},
        )
        assert isinstance(data, dict)
        return data

    async def create_webhook(self, channel_id: str, name: str) -> dict:
        data = await self.http.request(
            "POST", f"/channels/{channel_id}/webhooks", json_data={"name": name}
        )
        assert isinstance(data, dict)
        return data

    async def channel_webhooks(self, channel_id: str) -> list[dict]:
        data = await self.http.request("GET", f"/channels/{channel_id}/webhooks")
        assert isinstance(data, list)
        return data

    async def execute_webhook(
        self,
        webhook_id: str,
        webhook_token: str,
        *,
        content: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        embeds: list | None = None,
        components: list | None = None,
        wait: bool = False,
    ) -> dict | None:
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if username is not None:
            payload["username"] = username
        if avatar_url is not None:
            payload["avatar_url"] = avatar_url
        if embeds is not None:
            payload["embeds"] = _normalise_embeds(embeds)
        if components is not None:
            payload["components"] = components
        if not payload:
            raise ValueError("execute_webhook needs something to send")
        params = {"wait": "true"} if wait else None
        data = await self.http.request(
            "POST",
            f"/webhooks/{webhook_id}/{webhook_token}",
            json_data=payload,
            params=params,
        )
        # ``wait=false`` yields an empty body; the declared contract is a dict
        # or None, so normalise instead of leaking a ``list`` through.
        return data if isinstance(data, dict) else None

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.http.request("DELETE", f"/webhooks/{webhook_id}")

    async def delete_webhook_with_token(
        self, webhook_id: str, webhook_token: str
    ) -> None:
        await self.http.request("DELETE", f"/webhooks/{webhook_id}/{webhook_token}")

    # -- newsletters --------------------------------------------------
    async def newsletters(self) -> list[dict]:
        data = await self.http.request("GET", "/newsletters")
        assert isinstance(data, list)
        return data

    async def get_newsletter(self, newsletter_id: str) -> dict:
        data = await self.http.request("GET", f"/newsletters/{newsletter_id}")
        assert isinstance(data, dict)
        return data

    async def newsletter_message(
        self,
        newsletter_id: str,
        *,
        content: str | None = None,
        embeds: list | None = None,
        files: list | None = None,
    ) -> Message:
        fields: dict = {
            "content": content or "",
            "channel_id": newsletter_id,
        }
        if embeds is not None:
            fields["embedded"] = {"embeds": _normalise_embeds(embeds)}
        parts = {"payload_json": json.dumps(fields)}
        for index, item in enumerate(files or []):
            parts[f"files[{index}]"] = item
        data = await self.http.upload(
            f"/newsletters/{newsletter_id}/messages", fields=parts
        )
        assert isinstance(data, dict)
        return Message(data)

    # -- uploads ---------------------------------------------------------
    # Two distinct mechanisms, and the split matters:
    #   * avatars / banners / icons / emoji -> data URI inside a JSON body.
    #     The bytes are base64 either way, so multipart buys nothing.
    #   * webhook attachments -> genuine multipart, which Discord requires.
    async def _set_image(
        self,
        path: str,
        key: str,
        image: bytes,
        *,
        content_type: str,
        method: str = "PATCH",
    ) -> dict:
        payload: dict = {key: _image_data_uri(image, content_type)}
        data = await self.http.request(method, path, json_data=payload)
        assert isinstance(data, dict)
        return data

    async def set_avatar(
        self, image: bytes, *, content_type: str = "image/png"
    ) -> dict:
        """Set the profile picture. Pass ``b""`` to remove it."""
        return await self._set_image(
            "/users/@me", "avatar", image, content_type=content_type
        )

    async def set_banner(
        self, image: bytes, *, content_type: str = "image/png"
    ) -> dict:
        """Set the profile banner. Pass ``b""`` to remove it."""
        return await self._set_image(
            "/users/@me", "banner", image, content_type=content_type
        )

    async def set_display_name(self, name: str) -> dict:
        data = await self.http.request(
            "PATCH", "/users/@me", json_data={"global_name": name}
        )
        assert isinstance(data, dict)
        return data

    async def create_guild_emoji(
        self,
        guild_id: str,
        name: str,
        image: bytes,
        *,
        roles: list[str] | None = None,
        content_type: str = "image/png",
    ) -> dict:
        payload: dict = {
            "name": name,
            "image": _image_data_uri(image, content_type),
        }
        if roles is not None:
            payload["roles"] = roles
        data = await self.http.request(
            "POST", f"/guilds/{guild_id}/emojis", json_data=payload
        )
        assert isinstance(data, dict)
        return data

    async def set_guild_icon(
        self,
        guild_id: str,
        image: bytes,
        *,
        content_type: str = "image/png",
    ) -> dict:
        return await self._set_image(
            f"/guilds/{guild_id}", "icon", image, content_type=content_type
        )

    async def upload_webhook_file(
        self,
        webhook_id: str,
        webhook_token: str,
        file: bytes,
        *,
        filename: str = "upload.png",
        content_type: str = "application/octet-stream",
        content: str | None = None,
    ) -> dict:
        """Post a webhook message with a real attachment (multipart).

        This is the one upload path where multipart is mandatory: Discord
        pairs a ``payload_json`` field describing the attachment with a
        numbered ``files[N]`` part.
        """
        payload: dict = {"attachments": [{"id": 0, "filename": filename}]}
        if content is not None:
            payload["content"] = content
        data = await self.http.upload(
            f"/webhooks/{webhook_id}/{webhook_token}",
            fields={"payload_json": json.dumps(payload)},
            file_field="files[0]",
            filename=filename,
            data=file,
            content_type=content_type,
        )
        assert isinstance(data, dict)
        return data


def _image_data_uri(image: bytes, content_type: str) -> str | None:
    """Discord accepts avatars/banners as a data URI in the JSON payload.

    An empty body means "remove this image", which is a null payload value
    rather than an empty upload.
    """
    if not image:
        return None
    encoded = base64.b64encode(image).decode()
    return f"data:{content_type};base64,{encoded}"


# -- T3: presence (gateway-bound; throttle lives on Gateway) -------------
# The throttle is per-Gateway, not module-level: a shared global let two
# clients in one process stomp each other's 5s window.
async def set_presence(
    gateway, activities: list[dict], *, status: str = "online", force: bool = False
) -> None:
    """op3 rich presence. join/leave only (no per-channel voice tracking)."""
    await gateway.send_presence(activities, status=status, force=force)


async def clear_presence(gateway) -> None:
    await gateway.clear_presence()
