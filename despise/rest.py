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

from .gateway import PRESENCE_MIN_INTERVAL_S
from .http import DespiseHttp
from .models import Channel, Guild, Invite, Message, Relationship, User

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


class Rest:
    """Borrowed-http REST helpers. Stateless; Client owns one."""

    def __init__(self, http: DespiseHttp) -> None:
        self.http = http

    # -- T0 -----------------------------------------------------------
    async def me(self) -> User:
        data = await self.http.request("GET", "/users/@me")
        assert isinstance(data, dict)
        return User(data)

    # -- T1 -----------------------------------------------------------
    async def open_dm(self, user_id: str) -> Channel:
        data = await self.http.request(
            "POST", "/users/@me/channels", json_data={"recipient_id": user_id}
        )
        assert isinstance(data, dict)
        return Channel(data)

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        tts: bool = False,
        embeds: list | None = None,
        nonce: str | None = None,
        reply_to: str | None = None,
    ) -> Message:
        payload: dict = {"content": content, "tts": tts}
        if embeds is not None:
            payload["embeds"] = embeds
        if nonce is not None:
            payload["nonce"] = nonce
        if reply_to is not None:
            payload["message_reference"] = {"message_id": reply_to}
        data = await self.http.request(
            "POST",
            f"/channels/{channel_id}/messages",
            json_data=payload,
        )
        assert isinstance(data, dict)
        return Message(data)

    async def edit_message(
        self, channel_id: str, message_id: str, content: str
    ) -> Message:
        data = await self.http.request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json_data={"content": content},
        )
        assert isinstance(data, dict)
        return Message(data)

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self.http.request(
            "DELETE", f"/channels/{channel_id}/messages/{message_id}"
        )

    async def typing(self, channel_id: str) -> None:
        await self.http.request("POST", f"/channels/{channel_id}/typing")

    def typing_loop(self, channel_id: str, *, interval: float = 10.0) -> TypingLoop:
        """A long-running typing indicator.

        Discord expects a POST roughly every 10s while the user is typing, so
        the one-shot ``typing()`` above expires almost immediately::

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
        return [Message(m) for m in data]

    async def pin(self, channel_id: str, message_id: str) -> None:
        await self.http.request("PUT", f"/channels/{channel_id}/pins/{message_id}")

    async def unpin(self, channel_id: str, message_id: str) -> None:
        await self.http.request("DELETE", f"/channels/{channel_id}/pins/{message_id}")

    # -- T2 -----------------------------------------------------------
    async def accept_invite(self, code: str) -> dict:
        data = await self.http.request("POST", f"/invites/{code}", context="accept")
        assert isinstance(data, dict)
        return data

    async def get_guild(self, guild_id: str) -> Guild:
        data = await self.http.request("GET", f"/guilds/{guild_id}")
        assert isinstance(data, dict)
        return Guild(data)

    async def leave_guild(self, guild_id: str) -> None:
        await self.http.request("DELETE", f"/users/@me/guilds/{guild_id}")

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

    async def add_friend(self, user_id: str) -> Relationship:
        data = await self.http.request(
            "PUT",
            f"/users/@me/relationships/{user_id}",
            json_data={},
            context="friends",
        )
        assert isinstance(data, dict)
        return Relationship(data)

    async def remove_relationship(self, user_id: str) -> None:
        await self.http.request("DELETE", f"/users/@me/relationships/{user_id}")

    async def block(self, user_id: str) -> Relationship:
        data = await self.http.request(
            "PUT", f"/users/@me/relationships/{user_id}", json_data={"type": 2}
        )
        assert isinstance(data, dict)
        return Relationship(data)

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
        """
        await self.http.request("POST", "/interactions", json_data=payload)

    async def interaction_callback(
        self, interaction_id: str, token: str, response: dict
    ) -> None:
        """Answer an interaction. ``response`` comes from ``interactions.py``."""
        await self.http.request(
            "PUT",
            f"/interactions/{interaction_id}/{token}/callback",
            json_data=response,
        )

    async def edit_interaction_response(
        self,
        application_id: str,
        token: str,
        *,
        session_id: str | None = None,
        content: str | None = None,
        embeds: list | None = None,
        components: list | None = None,
    ) -> Message:
        """PATCH the original interaction response."""
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components
        if session_id is not None:
            payload["session_id"] = session_id
        data = await self.http.request(
            "PATCH",
            f"/webhooks/{application_id}/{token}/messages/@original",
            json_data=payload,
        )
        assert isinstance(data, dict)
        return Message(data)

    async def original_response(
        self, application_id: str, token: str, *, session_id: str | None = None
    ) -> Message:
        """GET the message an interaction created."""
        params = {"session_id": session_id} if session_id else None
        data = await self.http.request(
            "GET",
            f"/webhooks/{application_id}/{token}/messages/@original",
            params=params,
        )
        assert isinstance(data, dict)
        return Message(data)

    # -- T6: platform -------------------------------------------------
    async def get_invite(self, code: str) -> Invite:
        data = await self.http.request(
            "GET", f"/invites/{code}", params={"with_counts": "true"}
        )
        assert isinstance(data, dict)
        return Invite(data)

    async def follow_channel(self, channel_id: str, *, target_channel_id: str) -> Channel:
        data = await self.http.request(
            "POST",
            "/channels/followers",
            json_data={"channel_id": channel_id},
            params={"target_channel_id": target_channel_id},
        )
        assert isinstance(data, dict)
        return Channel(data)

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
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components
        if not payload:
            raise ValueError("execute_webhook needs something to send")
        params = {"wait": "true"} if wait else None
        return await self.http.request(
            "POST",
            f"/webhooks/{webhook_id}/{webhook_token}",
            json_data=payload,
            params=params,
        )

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
        fields = {
            "content": content or "",
            "channel_id": newsletter_id,
        }
        if embeds is not None:
            fields["embedded"] = {"embeds": embeds}
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
