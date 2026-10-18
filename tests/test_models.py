"""Model tests: equality/hash, timestamps, raw passthrough, rich parsing."""

from datetime import datetime, timezone

import pytest

from despise.models import (
    MESSAGE_FLAG_COMPONENTS_V2,
    MESSAGE_FLAG_EPHEMERAL,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    Attachment,
    Channel,
    Embed,
    Emoji,
    Guild,
    Invite,
    Member,
    Message,
    Reaction,
    ReactionEvent,
    Relationship,
    Role,
    User,
    VoiceState,
    parse_timestamp,
)


# -- identity ---------------------------------------------------------------
def test_equality_is_by_snowflake():
    a = User({"id": "1", "username": "a"})
    b = User({"id": "1", "username": "completely different"})
    c = User({"id": "2"})
    assert a == b
    assert a != c
    assert a == b


def test_hash_dedupes_in_sets_and_dicts():
    a = User({"id": "1"})
    b = User({"id": "1"})
    assert len({a, b}) == 1
    assert {a: "x"}[b] == "x"


def test_equality_spans_model_types():
    """Snowflakes are globally unique, so cross-type id equality is safe."""
    assert Member({"user": {"id": "1"}}) == User({"id": "1"})


def test_idless_models_are_never_equal():
    assert User(None) != User(None)
    assert User({}) != User({})


def test_id_is_a_string_attribute():
    assert User({"id": 123}).id == "123"
    assert User({}).id == ""


def test_comparison_to_foreign_types_is_not_an_error():
    assert (User({"id": "1"}) == "1") is False
    assert (User({"id": "1"}) == 1) is False


# -- timestamps -------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "2026-01-02T03:04:05Z",
        "2026-01-02T03:04:05.123456Z",
        "2026-01-02T03:04:05.123Z",
        "2026-01-02T03:04:05+00:00",
        "2026-01-02T03:04:05.123456+00:00",
        "2026-01-02T04:04:05+01:00",
    ],
)
def test_parse_timestamp_accepts_what_discord_sends(raw):
    parsed = parse_timestamp(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_timestamp_preserves_offset():
    a = parse_timestamp("2026-01-02T03:04:05Z")
    b = parse_timestamp("2026-01-02T04:04:05+01:00")
    assert a == b


def test_parse_timestamp_truncates_overlong_fractions():
    """Python 3.10's fromisoformat rejects >6 fractional digits."""
    parsed = parse_timestamp("2026-01-02T03:04:05.123456789+00:00")
    assert parsed is not None
    assert parsed.microsecond == 123456


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, [], "2026-13-45"])
def test_parse_timestamp_never_raises(bad):
    assert parse_timestamp(bad) is None


def test_datetime_passes_through():
    now = datetime.now(tz=timezone.utc)
    assert parse_timestamp(now) is now


def test_message_timestamps_are_parsed():
    m = Message({"id": "m", "timestamp": "2026-01-02T03:04:05.5Z"})
    assert isinstance(m.timestamp, datetime)
    assert m.timestamp.tzinfo is not None
    assert m.edited_timestamp is None


# -- raw --------------------------------------------------------------------
def test_raw_is_preserved_verbatim():
    payload = {"id": "1", "username": "t", "brand_new_discord_field": 42}
    assert User(payload).raw == payload
    assert User(payload).raw["brand_new_discord_field"] == 42


def test_raw_survives_a_missing_payload():
    assert User(None).raw == {}


# -- message ----------------------------------------------------------------
def test_message_author_compares_by_id():
    bot_user = User({"id": "9", "username": "me"})
    msg = Message({"id": "m", "author": {"id": "9", "username": "me"}})
    assert msg.author == bot_user


def test_message_nested_models():
    m = Message(
        {
            "id": "m",
            "channel_id": "c",
            "author": {"id": "9"},
            "content": "hi",
            "type": 16,
            "nonce": "n1",
            "flags": MESSAGE_FLAG_COMPONENTS_V2,
            "attachments": [{"id": "a", "filename": "x.png", "size": 10}],
            "embeds": [{"title": "T", "fields": [{"name": "n", "value": "v"}]}],
            "reactions": [{"count": 3, "me": True, "emoji": {"id": "e", "name": "x"}}],
            "mentions": [{"id": "u2"}],
            "mention_roles": ["r1"],
            "message_reference": {"message_id": "prev", "channel_id": "c"},
            "referenced_message": {"id": "prev", "content": "before"},
            "polls": [{"question": {"text": "q"}}],
            "member": {"nick": "nickname", "roles": ["r1"]},
        }
    )
    assert m.type == 16
    assert m.type_name == "reply"
    assert m.nonce == "n1"
    assert m.is_components_v2 is True
    assert isinstance(m.attachments[0], Attachment)
    assert m.attachments[0].filename == "x.png"
    assert isinstance(m.embeds[0], Embed)
    assert m.embeds[0].fields[0].name == "n"
    assert isinstance(m.reactions[0], Reaction)
    assert m.reactions[0].name == "x" and m.reactions[0].count == 3
    assert m.mentions[0].id == "u2"
    assert m.mention_roles == ["r1"]
    assert m.reference.message_id == "prev"
    assert m.referenced_message.content == "before"
    assert m.polls == [{"question": {"text": "q"}}]
    assert m.member.nick == "nickname"


def test_message_flag_helpers():
    assert Message({"flags": MESSAGE_FLAG_SUPPRESS_EMBEDS}).suppressed_embeds is True
    assert Message({"flags": MESSAGE_FLAG_EPHEMERAL}).is_ephemeral is True
    assert Message({}).suppressed_embeds is False


def test_reaction_event_keeps_context_and_survives_a_minimal_payload():
    ev = ReactionEvent(
        {
            "user_id": "u1",
            "message_id": "m1",
            "channel_id": "c1",
            "guild_id": "g1",
            "member": {"user": {"id": "u1", "username": "a"}},
            "emoji": {"id": "e1", "name": "ok"},
            "burst": True,
        }
    )
    assert (ev.user_id, ev.message_id) == ("u1", "m1")
    assert (ev.channel_id, ev.guild_id) == ("c1", "g1")
    assert ev.emoji_name == "ok"
    assert ev.member is not None and ev.member.id == "u1"
    assert ev.burst is True

    empty = ReactionEvent(None)
    assert empty.user_id == "" and empty.message_id == ""
    assert empty.member is None and empty.emoji is None
    assert empty.emoji_name == "" and empty.burst is False


def test_message_defaults_are_safe():
    m = Message({"id": "m"})
    # an absent author yields an empty User rather than an exception
    assert m.author.id == ""
    assert m.content == ""
    assert m.attachments == [] and m.embeds == [] and m.reactions == []
    assert m.clean_content == ""
    assert m.guild_id == ""


def test_clean_content_strips_mentions_and_markdown():
    m = Message(
        {
            "content": "hey <@123> and <#456> @everyone **bold** `code` ~~strike~~",
        }
    )
    clean = m.clean_content
    for token in ("<@123>", "<#456>", "@everyone", "**", "`", "~~"):
        assert token not in clean
    assert "hey" in clean and "bold" in clean


def test_malformed_nested_entries_are_skipped():
    m = Message({"id": "m", "attachments": ["nope", {"id": "a"}, None]})
    assert len(m.attachments) == 1


# -- channel / guild --------------------------------------------------------
def test_channel_extras():
    c = Channel(
        {
            "id": "c",
            "type": 0,
            "guild_id": "g",
            "name": "general",
            "topic": "hi",
            "position": 3,
            "parent_id": "p",
            "permission_overwrites": [{"id": "r", "allow": "1"}],
            "nsfw": True,
        }
    )
    assert c.topic == "hi" and c.position == 3 and c.parent_id == "p"
    assert c.permission_overwrites[0]["id"] == "r"
    assert c.nsfw is True
    assert c.is_dm is False and c.is_voice is False and c.is_thread is False


def test_channel_type_predicates():
    assert Channel({"type": 1}).is_dm
    assert Channel({"type": 3}).is_dm
    assert Channel({"type": 2}).is_voice
    assert Channel({"type": 11}).is_thread


def test_guild_extras():
    g = Guild(
        {
            "id": "g",
            "name": "G",
            "owner_id": "1",
            "member_count": 5,
            "features": ["COMMUNITY"],
            "channels": [{"id": "c1", "type": 0}],
            "members": [{"user": {"id": "u1"}}],
            "roles": [{"id": "r1", "name": "role"}],
            "emojis": [{"id": "e1", "name": "blob"}],
            "stickers": [{"id": "s1", "name": "wave"}],
        }
    )
    assert isinstance(g.channels[0], Channel)
    assert isinstance(g.members[0], Member)
    assert isinstance(g.roles[0], Role)
    assert g.features == ["COMMUNITY"]
    assert g.members[0].user.id == "u1"
    # guild stickers live under ``stickers``, not the message-only
    # ``sticker_items`` key -- reading the wrong one always returned [].
    assert [s.id for s in g.stickers] == ["s1"]


def test_guild_features_tolerates_a_string():
    assert Guild({"features": "not-a-list"}).features == []


# -- new models -------------------------------------------------------------
def test_member_falls_back_to_user_id():
    m = Member({"user": {"id": "u9"}, "nick": "n"})
    assert m.id == "u9"
    assert m.display_name == "n"
    assert Member({"user": {"id": "u9"}}).display_name == ""


def test_role_fields():
    r = Role({"id": "r", "name": "Mod", "color": 16711680, "position": 5})
    assert (r.name, r.color, r.position) == ("Mod", 16711680, 5)


def test_emoji_mention_and_url():
    e = Emoji({"id": "e", "name": "blob", "animated": True})
    assert e.mention == "<:blob:e>"
    assert Emoji({"name": "x"}).mention == "<:x>"


def test_invite_url_and_counts():
    i = Invite({"code": "abc123", "approximate_member_count": 10})
    assert i.url == "https://discord.gg/abc123"
    assert i.approximate_member_count == 10


def test_relationship_type_names():
    assert Relationship({"id": "u", "type": 1}).type_name == "friend"
    assert Relationship({"id": "u", "type": 2}).type_name == "blocked"
    assert Relationship({"id": "u", "type": 99}).type_name == "unknown"


def test_voice_state():
    v = VoiceState({"user_id": "u", "channel_id": "c", "self_mute": True})
    assert v.id == "u" and v.self_mute is True


def test_user_avatar_url():
    assert User({"id": "1", "avatar": "abc"}).avatar_url.endswith("/avatars/1/abc.png")
    assert User({"id": "1", "avatar": "a_abc"}).avatar_url.endswith(".gif")
    assert User({"id": "1"}).avatar_url is None


def test_user_display_name_prefers_global_name():
    assert User({"username": "u", "global_name": "G"}).display_name == "G"
    assert User({"username": "u"}).display_name == "u"
