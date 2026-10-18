"""Event-name routing (``events.route``) and payload conversion."""

import pytest

from despise import events
from despise.models import ReactionEvent


# -- routing ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Friendly names and their spellings route to a friendly bucket.
        ("message", ("friendly", "message", "MESSAGE_CREATE")),
        ("msg", ("friendly", "message", "MESSAGE_CREATE")),
        ("on_message", ("friendly", "message", "MESSAGE_CREATE")),
        ("on_ready", ("friendly", "ready", "READY")),
        # An exact dispatch name (upper or lower) is a raw subscription, so
        # the handler gets the payload dict -- use the friendly alias for a
        # model.
        ("ready", ("raw", "READY", "READY")),
        ("guild_member_add", ("raw", "GUILD_MEMBER_ADD", "GUILD_MEMBER_ADD")),
        ("on_guild_member_add", ("raw", "GUILD_MEMBER_ADD", "GUILD_MEMBER_ADD")),
        ("MESSAGE_CREATE", ("raw", "MESSAGE_CREATE", "MESSAGE_CREATE")),
        # An exact dispatch name stays raw even when a friendly alias exists:
        # the handler asked for the wire name, so it gets the dict.
        ("GUILD_BAN_ADD", ("raw", "GUILD_BAN_ADD", "GUILD_BAN_ADD")),
        ("THREAD_CREATE", ("raw", "THREAD_CREATE", "THREAD_CREATE")),
        # ...while the friendly alias resolves to the same dispatch.
        ("member_ban", ("friendly", "member_ban", "GUILD_BAN_ADD")),
        ("on_member_ban", ("friendly", "member_ban", "GUILD_BAN_ADD")),
        # ``thread_create`` is spelled like the dispatch, so it stays raw;
        # the ``on_`` decorator spelling is the friendly one.
        ("on_thread_create",
         ("friendly", "thread_create", "THREAD_CREATE")),
        # Dispatches with no entry at all must still route -- the regression.
        ("MESSAGE_POLL_VOTE_ADD", ("raw", "MESSAGE_POLL_VOTE_ADD",
                                   "MESSAGE_POLL_VOTE_ADD")),
        ("message_poll_vote_add", ("raw", "MESSAGE_POLL_VOTE_ADD",
                                   "MESSAGE_POLL_VOTE_ADD")),
    ],
)
def test_route(name, expected):
    assert events.route(name) == expected


def test_route_leaves_underscore_less_names_friendly():
    # No underscore means it cannot be a dispatch, so it stays a plain
    # friendly bucket rather than being invented as a gateway event.
    assert events.route("spawn") == ("friendly", "spawn", None)


# -- conversion -------------------------------------------------------------
def test_reaction_add_keeps_its_message_context():
    friendly, value = events.convert(
        "MESSAGE_REACTION_ADD",
        {
            "user_id": "u1",
            "message_id": "m1",
            "channel_id": "c1",
            "guild_id": "g1",
            "emoji": {"id": "e1", "name": "ok"},
        },
    )
    assert friendly == "reaction_add"
    assert isinstance(value, ReactionEvent)
    assert (value.user_id, value.message_id) == ("u1", "m1")
    assert (value.channel_id, value.guild_id) == ("c1", "g1")
    assert value.emoji_name == "ok"


def test_reaction_remove_converts_too():
    friendly, value = events.convert("MESSAGE_REACTION_REMOVE", {"message_id": "m1"})
    assert friendly == "reaction_remove"
    assert isinstance(value, ReactionEvent)
    assert value.message_id == "m1"


def test_unmapped_dispatch_converts_to_raw():
    payload = {"guild_id": "g1"}
    assert events.convert("MESSAGE_POLL_VOTE_ADD", payload) == (None, payload)


# -- coverage conversions ---------------------------------------------------
def test_guild_ban_converts_to_the_banned_user():
    friendly, value = events.convert(
        "GUILD_BAN_ADD", {"guild_id": "g1", "user": {"id": "u1", "username": "z"}}
    )
    assert friendly == "member_ban"
    assert value.id == "u1" and value.username == "z"


def test_thread_create_converts_to_a_channel():
    friendly, value = events.convert(
        "THREAD_CREATE", {"id": "t1", "type": 11, "name": "topic"}
    )
    assert friendly == "thread_create"
    assert value.id == "t1" and value.is_thread


def test_role_events_convert_to_a_role():
    friendly, value = events.convert(
        "GUILD_ROLE_CREATE",
        {"guild_id": "g1", "role": {"id": "r1", "name": "mod"}},
    )
    assert friendly == "role_create"
    assert value.id == "r1" and value.name == "mod"

    friendly, deleted = events.convert(
        "GUILD_ROLE_DELETE", {"guild_id": "g1", "role_id": "r1"}
    )
    assert friendly == "role_delete"
    assert deleted.id == "r1"


def test_user_update_converts_to_a_user():
    friendly, value = events.convert(
        "USER_UPDATE", {"id": "u1", "username": "new"}
    )
    assert friendly == "user_update"
    assert value.id == "u1" and value.username == "new"
