"""Tier tests: models + Rest paths/verbs + presence throttle + client boot."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from despise.client import Client
from despise.headers import EphemeralIds, build_profile
from despise.models import (
    Channel,
    Embed,
    Emoji,
    Guild,
    Member,
    Message,
    Role,
    User,
)
from despise.rest import File, Rest, build_activity, set_presence

EPH = EphemeralIds(client_launch_id="launch-1")


class Recorder:
    """Fake DespiseHttp.request recorder."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.profile = build_profile("web")
        self.eph = EPH

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responses.pop(0)

    async def upload(self, path, **kwargs):
        method = kwargs.pop("method", "POST")
        self.calls.append((method, path, kwargs))
        return self._responses.pop(0)


def run(coro):
    return asyncio.run(coro)


def rest_with(responses):
    rec = Recorder(responses)
    return Rest(rec), rec  # type: ignore[arg-type]


def test_models_slots():
    for cls in (User, Message, Channel, Guild):
        assert hasattr(cls, "__slots__")
    m = Message({"id": "1", "channel_id": "2", "author": {"username": "u"}})
    assert m.author.username == "u" and m.content == ""


def test_me_path():
    rest, rec = rest_with([{"id": "9", "username": "t"}])
    user = run(rest.me())
    assert isinstance(user, User) and user.id == "9"
    assert rec.calls[0][:2] == ("GET", "/users/@me")


def test_send_edit_delete_typing():
    msg = {"id": "m1", "channel_id": "c1", "content": "hi", "author": {"username": "u"}}
    rest, rec = rest_with([msg, {**msg, "content": "yo"}, {}, {}])
    assert run(rest.send_message("c1", "hi")).content == "hi"
    assert run(rest.edit_message("c1", "m1", "yo")).content == "yo"
    run(rest.delete_message("c1", "m1"))
    run(rest.typing("c1"))
    verbs = [(m, p) for m, p, _ in rec.calls]
    assert verbs[0] == ("POST", "/channels/c1/messages")
    assert verbs[1] == ("PATCH", "/channels/c1/messages/m1")
    assert verbs[2] == ("DELETE", "/channels/c1/messages/m1")
    assert verbs[3] == ("POST", "/channels/c1/typing")


def test_send_message_optional_payloads():
    msg = {"id": "m1", "channel_id": "c1", "author": {}}
    rest, rec = rest_with([msg])
    run(
        rest.send_message(
            "c1", "hi", embeds=[{"title": "t"}], nonce="abc", reply_to="m0"
        )
    )
    payload = rec.calls[0][2]["json_data"]
    assert payload["embeds"] == [{"title": "t"}]
    assert payload["nonce"] == "abc"
    assert payload["message_reference"] == {"message_id": "m0"}


def test_send_message_accepts_embed_models():
    rest, rec = rest_with([{"id": "m1", "channel_id": "c1", "author": {}}])
    run(rest.send_message("c1", embeds=[Embed({"title": "t", "color": 1})]))
    assert rec.calls[0][2]["json_data"]["embeds"] == [{"title": "t", "color": 1}]


def test_history_pagination_params():
    rest, rec = rest_with(
        [[{"id": "m1", "channel_id": "c", "author": {}}], {}, {}]
    )
    msgs = run(rest.history("c", limit=10, before="100"))
    assert len(msgs) == 1 and isinstance(msgs[0], Message)
    params = rec.calls[0][2]["params"]
    # repeated keys are needed for real pagination, so it is a pair list
    assert isinstance(params, list) and ("limit", "10") in params
    assert ("before", "100") in params
    run(rest.pin("c", "m1"))
    run(rest.unpin("c", "m1"))
    assert rec.calls[1][:2] == ("PUT", "/channels/c/messages/pins/m1")
    assert rec.calls[2][:2] == ("DELETE", "/channels/c/messages/pins/m1")


def test_history_omits_unset_pagination():
    rest, rec = rest_with([[]])
    run(rest.history("c", limit=5))
    assert rec.calls[0][2]["params"] == [("limit", "5")]


def test_open_dm_invite_guild_leave():
    rest, rec = rest_with(
        [
            {"id": "dm1", "type": 1, "recipients": []},
            {"code": "abc"},
            {"id": "g1", "name": "G"},
            {},
        ]
    )
    dm = run(rest.open_dm("u9"))
    assert isinstance(dm, Channel) and dm.is_dm
    inv = run(rest.accept_invite("abc"))
    assert inv == {"code": "abc"}
    assert rec.calls[1][2].get("context") == "accept"
    g = run(rest.get_guild("g1"))
    assert isinstance(g, Guild) and g.name == "G"
    run(rest.leave_guild("g1"))
    assert rec.calls[3][:2] == ("DELETE", "/users/@me/guilds/g1")


def test_subscribe_guild_delegates_to_gateway_method():
    """It used to reach into gateway._ws directly."""
    rest, _ = rest_with([])

    class FakeGw:
        def __init__(self):
            self.calls = []

        async def subscribe_guild(self, guild_id, *, channels=None):
            self.calls.append((guild_id, channels))

    gw = FakeGw()
    run(rest.subscribe_guild(gw, "g1", channels={"c1": {"ty": 2}}))
    assert gw.calls == [("g1", {"c1": {"ty": 2}})]


def test_relationships():
    rest, rec = rest_with(
        [
            [{"id": "u1", "type": 1, "user": {"id": "u1", "username": "z"}}],
            {"id": "u2", "type": 1},
            {},
            {"id": "u3", "type": 2},
        ]
    )
    rels = run(rest.relationships())
    assert [r.user_id for r in rels] == ["u1"]
    assert rels[0].type_name == "friend"
    assert rels[0].user.username == "z"

    added = run(rest.add_friend("u2", note="hi"))
    assert added is None
    assert rec.calls[1][2]["json_data"] == {"note": "hi"}
    assert rec.calls[1][2]["context"] == "friends"
    run(rest.remove_relationship("u2"))
    assert rec.calls[2][:2] == ("DELETE", "/users/@me/relationships/u2")
    blocked = run(rest.block("u3"))
    assert blocked is None
    assert rec.calls[3][1] == "/users/@me/relationships/u3"
    assert rec.calls[3][2]["json_data"] == {"type": 2}


def test_build_activity():
    act = build_activity("Chess", kind="competing", details="ranked")
    assert act == {"name": "Chess", "type": 5, "details": "ranked"}


def test_set_presence_delegates_to_gateway():
    class FakeGw:
        def __init__(self):
            self.calls = []

        async def send_presence(self, activities, status="online", force=False):
            self.calls.append((activities, status, force))

        async def clear_presence(self, status="online"):
            self.calls.append(([], status, True))

    from despise.rest import clear_presence

    gw = FakeGw()
    run(set_presence(gw, [{"name": "A", "type": 0}], status="idle"))
    run(clear_presence(gw))
    assert gw.calls == [
        ([{"name": "A", "type": 0}], "idle", False),
        ([], "online", True),
    ]


def test_client_boot_single_session():
    c = Client("tok", profile="web")
    assert c.http.profile == c.profile
    assert c.rest.http is c.http
    assert c.gateway.http is c.http  # gateway borrows the same DespiseHttp
    run(c.http.close())


# -- uploads ---------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n fake"


def test_set_avatar_sends_a_data_uri():
    rest, rec = rest_with([{"id": "9", "username": "t"}])
    run(rest.set_avatar(PNG))
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("PATCH", "/users/@me")
    uri = kwargs["json_data"]["avatar"]
    assert uri.startswith("data:image/png;base64,")
    import base64

    assert base64.b64decode(uri.split(",", 1)[1]) == PNG


def test_empty_image_clears_the_avatar():
    rest, rec = rest_with([{"id": "9"}])
    run(rest.set_avatar(b""))
    assert rec.calls[0][2]["json_data"] == {"avatar": None}


def test_set_banner_and_display_name():
    rest, rec = rest_with([{"id": "9"}, {"id": "9"}])
    run(rest.set_banner(PNG, content_type="image/jpeg"))
    run(rest.set_display_name("new name"))
    assert rec.calls[0][2]["json_data"]["banner"].startswith("data:image/jpeg;base64,")
    assert rec.calls[1][2]["json_data"] == {"global_name": "new name"}


def test_create_guild_emoji_includes_name_and_roles():
    """Regression: the name argument used to be silently dropped."""
    rest, rec = rest_with([{"id": "e1", "name": "blob"}])
    out = run(rest.create_guild_emoji("g1", "blob", PNG, roles=["r1", "r2"]))
    assert out["id"] == "e1"
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("POST", "/guilds/g1/emojis")
    payload = kwargs["json_data"]
    assert payload["name"] == "blob"
    assert payload["roles"] == ["r1", "r2"]
    assert payload["image"].startswith("data:image/png;base64,")


def test_set_guild_icon():
    rest, rec = rest_with([{"id": "g1"}])
    run(rest.set_guild_icon("g1", PNG))
    assert rec.calls[0][:2] == ("PATCH", "/guilds/g1")
    assert "icon" in rec.calls[0][2]["json_data"]


def test_webhook_file_upload_routes_to_the_webhook_endpoint():
    # The multipart body itself is asserted in test_http.py against the real
    # DespiseHttp.upload; here we only care about routing and fields.
    rest, rec = rest_with([{"id": "m1"}])
    run(
        rest.upload_webhook_file(
            "w1", "tok", PNG, filename="pic.png", content="here"
        )
    )
    method, path, kwargs = rec.calls[0]
    assert method == "POST"
    assert path == "/webhooks/w1/tok"
    assert "here" in kwargs["fields"]["payload_json"]


def test_webhook_upload_builds_a_payload_json_field():
    import json as _json

    rest, rec = rest_with([{}])
    run(rest.upload_webhook_file("w1", "tok", PNG, filename="pic.png"))
    fields = rec.calls[0][2]["fields"]
    payload = _json.loads(fields["payload_json"])
    assert payload["attachments"] == [{"id": 0, "filename": "pic.png"}]
    assert rec.calls[0][2]["file_field"] == "files[0]"
    assert rec.calls[0][2]["data"] == PNG


# -- unified send: files / components / poll -------------------------------
def test_send_message_with_files_builds_multipart():
    msg = {"id": "m1", "channel_id": "c1", "author": {}}
    rest, rec = rest_with([msg])
    run(
        rest.send_message(
            "c1",
            "look",
            files=[
                File(PNG, filename="a.png", content_type="image/png"),
                "b.txt",
            ],
        )
    )
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("POST", "/channels/c1/messages")
    payload = json.loads(kwargs["fields"]["payload_json"])
    assert payload["content"] == "look"
    # attachments must line up with the files[N] parts, in order
    assert payload["attachments"] == [
        {"id": 0, "filename": "a.png"},
        {"id": 1, "filename": "b.txt"},
    ]
    assert [f["field"] for f in kwargs["files"]] == ["files[0]", "files[1]"]
    assert kwargs["files"][0]["content_type"] == "image/png"


def test_send_message_forwards_components_poll_and_stickers():
    msg = {"id": "m1", "channel_id": "c1", "author": {}}
    rest, rec = rest_with([msg])
    run(
        rest.send_message(
            "c1",
            components=[{"type": 1, "components": []}],
            poll={"question": {"text": "q"}},
            stickers=["s1"],
            allowed_mentions={"parse": []},
        )
    )
    payload = rec.calls[0][2]["json_data"]
    assert payload["components"] == [{"type": 1, "components": []}]
    assert payload["poll"] == {"question": {"text": "q"}}
    assert payload["sticker_ids"] == ["s1"]
    assert payload["allowed_mentions"] == {"parse": []}
    # no files -> plain JSON, no multipart
    assert "files" not in rec.calls[0][2]


def test_edit_message_supports_embeds_and_components():
    rest, rec = rest_with([{"id": "m1", "author": {}}])
    run(
        rest.edit_message(
            "c1", "m1", embeds=[{"title": "t"}], components=[{"type": 1}]
        )
    )
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("PATCH", "/channels/c1/messages/m1")
    assert kwargs["json_data"]["embeds"] == [{"title": "t"}]
    assert kwargs["json_data"]["components"] == [{"type": 1}]


def test_fetch_message():
    rest, rec = rest_with([{"id": "m1", "channel_id": "c1", "author": {}}])
    msg = run(rest.fetch_message("c1", "m1"))
    assert isinstance(msg, Message) and msg.id == "m1"
    assert rec.calls[0][:2] == ("GET", "/channels/c1/messages/m1")


# -- reactions -------------------------------------------------------------
def test_reaction_paths_quote_the_emoji():
    rest, rec = rest_with([{}, {}, {}, {}, {}])
    run(rest.add_reaction("c", "m", "\U0001f44d"))
    run(rest.remove_own_reaction("c", "m", "\U0001f44d"))
    run(rest.remove_reaction("c", "m", "\U0001f44d", "u1"))
    run(rest.clear_reactions("c", "m"))
    run(rest.clear_reactions("c", "m", emoji="<:party:123>"))
    verbs = [(m, p) for m, p, _ in rec.calls]
    assert verbs[0] == ("PUT", "/channels/c/messages/m/reactions/%F0%9F%91%8D/@me")
    assert verbs[1] == ("DELETE", "/channels/c/messages/m/reactions/%F0%9F%91%8D/@me")
    assert verbs[2] == ("DELETE", "/channels/c/messages/m/reactions/%F0%9F%91%8D/u1")
    assert verbs[3] == ("DELETE", "/channels/c/messages/m/reactions")
    assert verbs[4] == ("DELETE", "/channels/c/messages/m/reactions/party%3A123")


def test_emoji_param_accepts_models_and_mentions():
    from despise.models import Emoji
    from despise.rest import _emoji_param

    assert _emoji_param(Emoji({"id": "123", "name": "party"})) == "party:123"
    assert _emoji_param(Emoji({"name": "wave"})) == "wave"
    assert _emoji_param("<a:dance:456>") == "dance:456"
    assert _emoji_param("\U0001f525") == "\U0001f525"


# -- File normalisation ----------------------------------------------------
def test_file_helpers_normalise_inputs(tmp_path):
    from despise.rest import _resolve_files

    path = tmp_path / "note.txt"
    path.write_bytes(b"hi")

    assert [f.filename for f in _resolve_files(File.from_path(path))] == ["note.txt"]
    assert [f.filename for f in _resolve_files([b"data"])] == ["file.bin"]
    assert [f.filename for f in _resolve_files([("a.txt", b"x", "text/plain")])] == [
        "a.txt"
    ]
    assert [f.filename for f in _resolve_files([str(path)])] == ["note.txt"]
    assert _resolve_files(None) == []
    with pytest.raises(ValueError):
        File()
    with pytest.raises(ValueError):
        File(b"x", local_path="y")


# -- model-bound actions ---------------------------------------------------
class StubRest:
    def __init__(self):
        self.calls = []

    async def send_message(self, cid, content=None, **kw):
        self.calls.append(("send", cid, content, kw))
        return "sent"

    async def edit_message(self, cid, mid, content=None, **kw):
        self.calls.append(("edit", cid, mid, content, kw))
        return "edited"

    async def delete_message(self, cid, mid, **kw):
        self.calls.append(("delete", cid, mid, kw))

    async def add_reaction(self, cid, mid, emoji):
        self.calls.append(("react", cid, mid, emoji))

    async def remove_own_reaction(self, cid, mid, emoji):
        self.calls.append(("unreact", cid, mid, emoji))

    async def clear_reactions(self, cid, mid, *, emoji=None):
        self.calls.append(("clear", cid, mid, emoji))

    async def fetch_message(self, cid, mid):
        self.calls.append(("fetch", cid, mid))
        return "fetched"


class StubClient:
    def __init__(self):
        self.rest = StubRest()


def test_message_actions_delegate_to_rest():
    client = StubClient()
    msg = Message({"id": "m1", "channel_id": "c1", "author": {}})._bind(client)
    run(msg.reply("hi"))
    assert client.rest.calls[-1] == ("send", "c1", "hi", {"reply_to": "m1"})
    run(msg.edit("new"))
    assert client.rest.calls[-1] == ("edit", "c1", "m1", "new", {})
    run(msg.react("\U0001f44d"))
    assert client.rest.calls[-1] == ("react", "c1", "m1", "\U0001f44d")
    run(msg.remove_reaction("\U0001f44d"))
    assert client.rest.calls[-1] == ("unreact", "c1", "m1", "\U0001f44d")
    run(msg.clear_reactions())
    assert client.rest.calls[-1] == ("clear", "c1", "m1", None)
    run(msg.delete())
    assert client.rest.calls[-1][:3] == ("delete", "c1", "m1")


def test_channel_actions_delegate_to_rest():
    client = StubClient()
    channel = Channel({"id": "c1", "type": 0})._bind(client)
    run(channel.send("hey", embeds=[{"title": "t"}]))
    assert client.rest.calls[-1] == ("send", "c1", "hey", {"embeds": [{"title": "t"}]})
    run(channel.fetch_message("m1"))
    assert client.rest.calls[-1] == ("fetch", "c1", "m1")


def test_unbound_model_actions_raise():
    msg = Message({"id": "m1", "channel_id": "c1", "author": {}})
    with pytest.raises(RuntimeError, match="not attached to a Client"):
        run(msg.reply("hi"))


def test_rest_binds_models_when_it_has_a_client():
    client = StubClient()
    rec = Recorder([{"id": "m1", "channel_id": "c1", "author": {}}])
    rest = Rest(rec, client=client)  # type: ignore[arg-type]
    msg = run(rest.send_message("c1", "hi"))
    assert msg._client is client


# -- read state ------------------------------------------------------------
# Routes verified against https://docs.discord.food/topics/read-state
def test_ack_message_path_and_payload():
    rest, rec = rest_with([{}])
    run(rest.ack_message("c1", "m1", manual=True, mention_count=3, last_viewed=5))
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("POST", "/channels/c1/messages/m1/ack")
    assert kwargs["json_data"] == {
        "token": None,
        "manual": True,
        "mention_count": 3,
        "last_viewed": 5,
    }


def test_ack_message_omits_unset_fields():
    rest, rec = rest_with([{}])
    run(rest.ack_message("c1", "m1"))
    # ``token: null`` is what the real clients send; everything else is left out.
    assert rec.calls[0][2]["json_data"] == {"token": None}


def test_read_state_paths():
    rest, rec = rest_with([{}, {}, {}, {}, {}])
    run(rest.ack_pins("c1"))
    run(rest.ack_guild("g1"))
    run(rest.ack_guild_feature("g1", 2, "e1"))
    run(rest.ack_user_feature(1, "m1"))
    run(rest.delete_read_state("c1", read_state_type=1))
    verbs = [(m, p) for m, p, _ in rec.calls]
    assert verbs == [
        ("POST", "/channels/c1/pins/ack"),
        ("POST", "/guilds/g1/ack"),
        ("POST", "/guilds/g1/ack/2/e1"),
        ("POST", "/users/@me/1/m1/ack"),
        ("DELETE", "/channels/c1/messages/ack"),
    ]
    assert rec.calls[4][2]["json_data"] == {"read_state_type": 1}


def test_ack_bulk_wraps_read_states():
    rest, rec = rest_with([{}])
    run(rest.ack_bulk([{"channel_id": "c1", "message_id": "m1"}]))
    method, path, kwargs = rec.calls[0]
    assert (method, path) == ("POST", "/read-states/ack-bulk")
    assert kwargs["json_data"] == {
        "read_states": [{"channel_id": "c1", "message_id": "m1"}]
    }


def test_message_ack_action_delegates():
    client = StubClient()

    class AckStub:
        def __init__(self):
            self.calls = []

        async def ack_message(self, cid, mid, **kw):
            self.calls.append((cid, mid, kw))

    client.rest = AckStub()
    msg = Message({"id": "m1", "channel_id": "c1", "author": {}})._bind(client)
    run(msg.ack(manual=True))
    assert client.rest.calls == [("c1", "m1", {"manual": True})]


# -- REST breadth ----------------------------------------------------------
def test_channel_breadth_paths():
    rest, rec = rest_with(
        [
            {"id": "c1", "type": 0},
            {"id": "c1", "name": "renamed"},
            {},
            {},
            {},
            {},
        ]
    )
    assert run(rest.get_channel("c1")).id == "c1"
    edited = run(rest.edit_channel("c1", name="renamed", topic="t"))
    assert edited.name == "renamed"
    run(rest.delete_channel("c1", reason="bye"))
    run(rest.edit_channel_permissions("c1", "r1", allow="8", deny=0, type=0))
    run(rest.delete_channel_permission("c1", "r1"))
    run(rest.remove_recipient("c1", "u1"))
    verbs = [(m, p) for m, p, _ in rec.calls]
    assert verbs == [
        ("GET", "/channels/c1"),
        ("PATCH", "/channels/c1"),
        ("DELETE", "/channels/c1"),
        ("PUT", "/channels/c1/permissions/r1"),
        ("DELETE", "/channels/c1/permissions/r1"),
        ("DELETE", "/channels/c1/recipients/u1"),
    ]
    assert rec.calls[1][2]["json_data"] == {"name": "renamed", "topic": "t"}
    assert rec.calls[2][2]["reason"] == "bye"
    # permission bitfields are stringified for Discord
    assert rec.calls[3][2]["json_data"] == {
        "type": 0,
        "allow": "8",
        "deny": "0",
    }


def test_group_dm_and_private_channels():
    rest, rec = rest_with(
        [
            {"id": "g1", "type": 3, "recipients": []},
            [{"id": "c1", "type": 1}, {"id": "g1", "type": 3}],
        ]
    )
    dm = run(rest.create_group_dm(["u1", "u2"], nicks={"u1": "nick"}))
    assert isinstance(dm, Channel) and dm.is_dm
    assert rec.calls[0][2]["json_data"] == {
        "recipients": ["u1", "u2"],
        "nicks": {"u1": "nick"},
    }
    chans = run(rest.private_channels())
    assert [c.id for c in chans] == ["c1", "g1"]


def test_pinned_messages_unwraps_pin_items():
    rest, rec = rest_with(
        [
            {
                "items": [
                    {
                        "pinned_at": "2026-10-20T00:00:00+00:00",
                        "message": {"id": "m1", "author": {}},
                    }
                ],
                "has_more": False,
            }
        ]
    )
    pins = run(rest.pinned_messages("c1", limit=5))
    assert [m.id for m in pins] == ["m1"]
    assert rec.calls[0][2]["params"] == [("limit", "5")]


def test_crosspost_message():
    rest, rec = rest_with([{"id": "m1", "channel_id": "c1", "author": {}}])
    msg = run(rest.crosspost_message("c1", "m1"))
    assert msg.id == "m1"
    assert rec.calls[0][:2] == ("POST", "/channels/c1/messages/m1/crosspost")


def test_guild_member_and_role_admin_paths():
    rest, rec = rest_with(
        [
            {"user": {"id": "u1", "username": "z"}, "roles": []},
            {"user": {"id": "u1"}, "roles": ["r1"]},
            {},
            {},
            {},
            [{"user": {"id": "u2"}, "reason": None}],
            {},
            {},
            {"id": "r1", "name": "mod"},
            {"id": "r1", "name": "mod2"},
            {},
        ]
    )
    member = run(rest.get_member("g1", "u1"))
    assert isinstance(member, Member) and member.id == "u1"
    assert member.guild_id == "g1"
    edited = run(rest.edit_member("g1", "u1", nick="n", roles=["r1"]))
    assert edited.nick is None and edited.roles == ["r1"]
    run(rest.kick_member("g1", "u1"))
    run(rest.add_member_role("g1", "u1", "r1"))
    run(rest.remove_member_role("g1", "u1", "r1"))
    bans = run(rest.bans("g1"))
    assert bans[0]["user"]["id"] == "u2"
    run(rest.ban("g1", "u2", delete_message_seconds=3600))
    run(rest.unban("g1", "u2"))
    role = run(rest.create_role("g1", name="mod", permissions=8))
    assert isinstance(role, Role) and role.name == "mod"
    run(rest.edit_role("g1", "r1", name="mod2"))
    run(rest.delete_role("g1", "r1"))
    verbs = [(m, p) for m, p, _ in rec.calls]
    assert verbs[0] == ("GET", "/guilds/g1/members/u1")
    assert verbs[1] == ("PATCH", "/guilds/g1/members/u1")
    assert verbs[2] == ("DELETE", "/guilds/g1/members/u1")
    assert verbs[3] == ("PUT", "/guilds/g1/members/u1/roles/r1")
    assert verbs[4] == ("DELETE", "/guilds/g1/members/u1/roles/r1")
    assert verbs[5] == ("GET", "/guilds/g1/bans")
    assert verbs[6] == ("PUT", "/guilds/g1/bans/u2")
    assert verbs[7] == ("DELETE", "/guilds/g1/bans/u2")
    assert verbs[8] == ("POST", "/guilds/g1/roles")
    assert verbs[9] == ("PATCH", "/guilds/g1/roles/r1")
    assert verbs[10] == ("DELETE", "/guilds/g1/roles/r1")
    # user accounts must send an explicit limit on the bans list
    assert rec.calls[5][2]["params"] == [("limit", "1000")]
    assert rec.calls[6][2]["json_data"] == {"delete_message_seconds": 3600}
    assert rec.calls[8][2]["json_data"]["permissions"] == "8"


def test_guild_preview_and_channels():
    rest, rec = rest_with(
        [{"id": "g1", "name": "G"}, [{"id": "c1", "type": 0}]]
    )
    preview = run(rest.guild_preview("g1"))
    assert isinstance(preview, Guild) and preview.name == "G"
    chans = run(rest.guild_channels("g1"))
    assert [c.id for c in chans] == ["c1"]
    assert rec.calls[1][:2] == ("GET", "/guilds/g1/channels")


def test_user_profile_and_notes():
    rest, rec = rest_with(
        [{"user": {"id": "u1"}}, {"note": "hi"}, {}]
    )
    profile = run(rest.user_profile("u1", guild_id="g1", with_mutual_guilds=False))
    assert profile["user"]["id"] == "u1"
    assert rec.calls[0][2]["params"] == [
        ("guild_id", "g1"),
        ("with_mutual_guilds", "false"),
    ]
    assert run(rest.get_note("u1")) == "hi"
    run(rest.set_note("u1", "bye"))
    assert rec.calls[2][2]["json_data"] == {"note": "bye"}


def test_edit_profile_and_bio():
    rest, rec = rest_with([{"bio": "hi"}, {}])
    run(rest.edit_profile(bio="hi", pronouns="they/them", accent_color=0xFF0000))
    run(rest.set_bio(None))
    assert rec.calls[0][:2] == ("PATCH", "/users/@me/profile")
    assert rec.calls[0][2]["json_data"] == {
        "bio": "hi",
        "pronouns": "they/them",
        "accent_color": 0xFF0000,
    }
    assert rec.calls[1][2]["json_data"] == {"bio": None}


def test_avatar_decoration_and_display_name_style():
    rest, rec = rest_with([{}, {}])
    run(rest.set_avatar_decoration("sku1"))
    run(
        rest.set_display_name_style(
            font_id=3, effect_id=2, colors=[0xFF0000, 0x00FF00]
        )
    )
    assert rec.calls[0][:2] == ("PATCH", "/users/@me")
    assert rec.calls[0][2]["json_data"] == {"avatar_decoration_sku_id": "sku1"}
    assert rec.calls[1][2]["json_data"] == {
        "display_name_font_id": 3,
        "display_name_effect_id": 2,
        "display_name_colors": [0xFF0000, 0x00FF00],
    }


def test_guild_profile_and_nickname():
    rest, rec = rest_with(
        [{"guild_id": "g1", "bio": "guild bio"}, {}, {"nick": "nick", "user": {"id": "u1"}}]
    )
    profile = run(rest.edit_guild_profile("g1", bio="guild bio", pronouns="any"))
    run(rest.set_guild_bio("g1", None))
    member = run(rest.set_guild_nickname("g1", "nick", reason="rename"))
    assert profile["bio"] == "guild bio"
    assert rec.calls[0][:2] == ("PATCH", "/guilds/g1/profile/@me")
    assert rec.calls[0][2]["json_data"] == {"bio": "guild bio", "pronouns": "any"}
    assert rec.calls[1][2]["json_data"] == {"bio": None}
    assert rec.calls[2][:2] == ("PATCH", "/guilds/g1/members/@me")
    assert rec.calls[2][2]["json_data"] == {"nick": "nick"}
    assert rec.calls[2][2]["reason"] == "rename"
    assert isinstance(member, Member)
    assert member.nick == "nick"
    assert member.guild_id == "g1"


def test_custom_status_replaces_the_whole_status():
    rest, rec = rest_with([{}, {}])
    expires = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    run(rest.set_custom_status("working", emoji="🛠️", expires_at=expires))
    run(rest.set_custom_status(None))
    first = rec.calls[0][2]["json_data"]["custom_status"]
    assert rec.calls[0][:2] == ("PATCH", "/users/@me/settings")
    assert first == {
        "text": "working",
        "emoji_id": None,
        "emoji_name": "🛠️",
        "expires_at": "2026-01-02T03:04:00+00:00",
    }
    assert rec.calls[1][2]["json_data"]["custom_status"] == {
        "text": None,
        "emoji_id": None,
        "emoji_name": None,
        "expires_at": None,
    }


def test_custom_status_accepts_custom_emoji_variants():
    rest, rec = rest_with([{}, {}, {}])
    run(rest.set_custom_status("a", emoji=Emoji({"id": "e1", "name": "blob"})))
    run(rest.set_custom_status("b", emoji="<:blob:e1>"))
    run(rest.set_custom_status("c", emoji="blob:e1"))
    for call in rec.calls:
        status = call[2]["json_data"]["custom_status"]
        assert status["emoji_id"] == "e1"
        assert status["emoji_name"] is None


def test_get_note_defaults_to_empty_string():
    rest, _ = rest_with([{}])
    assert run(rest.get_note("u1")) == ""
