"""Tier tests: models + Rest paths/verbs + presence throttle + client boot."""

import asyncio

from despise.client import Client
from despise.headers import EphemeralIds, build_profile
from despise.models import Channel, Guild, Message, Relationship, User
from despise.rest import Rest, build_activity, set_presence

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
    assert rec.calls[1][:2] == ("PUT", "/channels/c/pins/m1")
    assert rec.calls[2][:2] == ("DELETE", "/channels/c/pins/m1")


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

    added = run(rest.add_friend("u2"))
    assert isinstance(added, Relationship)
    assert rec.calls[1][2]["context"] == "friends"
    run(rest.remove_relationship("u2"))
    assert rec.calls[2][:2] == ("DELETE", "/users/@me/relationships/u2")
    blocked = run(rest.block("u3"))
    assert rec.calls[3][1] == "/users/@me/relationships/u3"
    assert blocked.type_name == "blocked"


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
