"""Command modules: prefix commands, listeners, load/unload/reload."""

from pathlib import Path

import pytest
from test_client import patched, run

from despise.cmd import CheckFailure, Module, UsageError, command, listen, require
from despise.cmd.checks import has_role, in_channel, in_dm, in_guild, is_owner
from despise.exceptions import DespiseError, NotFound
from despise.models import Channel, Guild, Member, Message, Role, User


def msg_payload(content, author_id="u1", guild_id=None):
    return {
        "id": "m1",
        "channel_id": "c1",
        "guild_id": guild_id,
        "content": content,
        "author": {"id": author_id, "username": "tester"},
    }


class Ping(Module):
    name = "pingmod"
    seen = []

    @command("ping", aliases=["p"])
    async def ping(self, ctx):
        Ping.seen.append((ctx.name, ctx.args))

    @listen("message")
    async def any_message(self, msg):
        Ping.seen.append(("got", msg.content))


def test_command_runs_and_args_parse(monkeypatch):
    Ping.seen.clear()
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    run(gw._emit("MESSAGE_CREATE", msg_payload("!ping hello world")))
    assert ("ping", ["hello", "world"]) in Ping.seen
    run(c.close())


def test_alias_and_case_and_quotes(monkeypatch):
    Ping.seen.clear()
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    run(gw._emit("MESSAGE_CREATE", msg_payload('!P "a b" c')))
    assert ("p", ["a b", "c"]) in Ping.seen
    run(c.close())


def test_non_command_messages_ignored(monkeypatch):
    Ping.seen.clear()
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    run(gw._emit("MESSAGE_CREATE", msg_payload("just chatting")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!unknown thing")))
    assert [s for s in Ping.seen if s[0] == "ping"] == []
    run(c.close())


def test_listener_fires_and_unload_removes_everything(monkeypatch):
    Ping.seen.clear()
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    run(gw._emit("MESSAGE_CREATE", msg_payload("hi")))
    assert ("got", "hi") in Ping.seen
    c.modules.unload("pingmod")
    Ping.seen.clear()
    run(gw._emit("MESSAGE_CREATE", msg_payload("!ping x")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("hi again")))
    assert Ping.seen == []
    run(c.close())


def test_double_load_and_unknown_unload_fail(monkeypatch):
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    with pytest.raises(ValueError, match="already loaded"):
        c.modules.load(Ping)
    with pytest.raises(KeyError):
        c.modules.unload("nope")
    run(c.close())


def test_command_errors_reach_on_error(monkeypatch):
    class Boom(Module):
        @command("boom")
        async def boom(self, ctx):
            raise RuntimeError("kaput")

    c, gw = patched(monkeypatch)
    errors = []
    c.on_error(lambda event, exc: errors.append((event, exc)))
    c.modules.load(Boom)
    run(gw._emit("MESSAGE_CREATE", msg_payload("!boom")))
    assert errors and errors[0][0] == "command:boom"
    assert isinstance(errors[0][1], RuntimeError)
    run(c.close())


def test_scoped_on_error_only_hears_its_command(monkeypatch):
    class Boom(Module):
        @command("boom")
        async def boom(self, ctx):
            raise RuntimeError("kaput")

        @command("fine")
        async def fine(self, ctx):
            raise RuntimeError("also bad")

    c, gw = patched(monkeypatch)
    c.modules.load(Boom)
    heard = []
    other = []

    async def scoped(name, exc):
        heard.append((name, str(exc)))

    async def global_(name, exc):
        other.append(name)

    c.on_error(scoped, event="command:boom")
    c.on_error(global_)
    run(gw._emit("MESSAGE_CREATE", msg_payload("!boom")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!fine")))
    assert heard == [("command:boom", "kaput")]
    assert sorted(other) == ["command:boom", "command:fine"]
    run(c.close())


def test_load_from_file_path(monkeypatch):
    path = str(Path(__file__).resolve().parent.parent / "cmd" / "general.py")
    c, gw = patched(monkeypatch)
    sent = []

    async def _sent(channel_id, content, **kwargs):
        sent.append((channel_id, content))
        return {"id": "x"}

    monkeypatch.setattr(c.rest, "send_message", _sent)
    mod = c.modules.load(path)
    assert mod.module_name == "general"
    assert "ping" in c.modules._commands
    assert "p" in c.modules._commands
    run(gw._emit("MESSAGE_CREATE", msg_payload("!ping hi")))
    reloaded = c.modules.reload("general")
    assert reloaded.module_name == "general"
    assert "ping" in c.modules._commands
    c.modules.unload("general")
    assert "ping" not in c.modules._commands
    run(c.close())


def test_dispatcher_survives_anything_users_type(monkeypatch):
    Ping.seen.clear()
    c, gw = patched(monkeypatch)
    c.modules.load(Ping)
    errors = []
    c.on_error(lambda event, exc: errors.append((event, exc)))
    nasty = [
        "!",
        "!   ",
        "!*",
        "!***",
        "!say \"unbalanced",
        "!say 'unbalanced",
        "!caf\u00e9 au lait",
        "!ping h\u00e9llo\u2192w\u00f6rld \U0001f389",
        "!\U0001f600\U0001f600\U0001f600",
        "!ping \u200b\u200c\u200d \u202ereversed\u202c done",
        "!ping \"tab\there\" newline\ninside",
        "!ping " + "a" * 5000,
        "!ping \\backslash\\ \\*star\\* \\!bang",
        "!\u00abquoted\u00bb !'nested \"quotes\"'",
        "!\t\n ",
        "!!ping",
        "!PING",
        "! \u0301\u0302\u0303 combining only",
    ]
    for text in nasty:
        run(gw._emit("MESSAGE_CREATE", msg_payload(text)))
    assert errors == []
    assert ("ping", ["h\u00e9llo\u2192w\u00f6rld", "\U0001f389"]) in Ping.seen
    run(c.close())


class Typed(Module):
    seen = []
    errors = []

    @command("add")
    async def add(self, ctx, x: int, y: int = 10):
        Typed.seen.append(("add", x + y))

    @command("half")
    async def half(self, ctx, v: float):
        Typed.seen.append(("half", v / 2))

    @command("flag")
    async def flag(self, ctx, on: bool):
        Typed.seen.append(("flag", on))

    @command("say")
    async def say(self, ctx, *, text: str):
        Typed.seen.append(("say", text))

    @command("sum")
    async def total(self, ctx, *nums: int):
        Typed.seen.append(("sum", sum(nums)))

    @command("who")
    async def who(self, ctx, member: Member):
        Typed.seen.append(("who", member.id))

    @command("u")
    async def user_of(self, ctx, user: User):
        Typed.seen.append(("u", user.id))

    @command("ch")
    async def channel_of(self, ctx, channel: Channel):
        Typed.seen.append(("ch", channel.id))

    @command("role")
    async def role_of(self, ctx, role: Role):
        Typed.seen.append(("role", role.id))

    @command("srv")
    async def guild_of(self, ctx, guild: Guild):
        Typed.seen.append(("srv", guild.id))

    @command("maybe")
    async def maybe(self, ctx, name: str | None = None):
        Typed.seen.append(("maybe", name))


def load_typed(monkeypatch):
    c, gw = patched(monkeypatch)
    c.modules.load(Typed)
    c.on_error(lambda event, exc: Typed.errors.append((event, exc)))

    async def _offline(*args, **kwargs):
        raise DespiseError("offline")

    monkeypatch.setattr(c.http, "request", _offline)
    c.cache.put_user(User({"id": "u3", "username": "Uma"}))
    c.cache.put_channel(Channel({"id": "c9", "name": "general", "guild_id": "g1"}))
    c.cache.put_member(
        Member({"user": {"id": "u2", "username": "Zed"}, "guild_id": "g1", "nick": "Zeddy"})
    )
    c.cache.put_guild(
        Guild(
            {
                "id": "g1",
                "name": "Test",
                "roles": [{"id": "r1", "name": "Mods"}],
                "members": [
                    {"user": {"id": "u2", "username": "Zed"}, "guild_id": "g1", "nick": "Zeddy"}
                ],
                "channels": [{"id": "c9", "name": "general", "guild_id": "g1"}],
            }
        )
    )
    return c, gw


def guild_msg(content):
    return msg_payload(content, guild_id="g1")


def test_typed_scalars_defaults_and_usage(monkeypatch):
    Typed.seen.clear()
    Typed.errors.clear()
    c, gw = load_typed(monkeypatch)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!add 3 4")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!add 3")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!half 5")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!flag yes")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!flag off")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!maybe")))
    assert ("add", 7) in Typed.seen
    assert ("add", 13) in Typed.seen
    assert ("half", 2.5) in Typed.seen
    assert ("flag", True) in Typed.seen
    assert ("flag", False) in Typed.seen
    assert ("maybe", None) in Typed.seen
    assert Typed.errors == []

    run(gw._emit("MESSAGE_CREATE", guild_msg("!add")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!add x y")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!half abc")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!flag maybe")))
    assert len(Typed.errors) == 4
    assert all(isinstance(exc, UsageError) for _, exc in Typed.errors)
    assert [event for event, _ in Typed.errors] == [
        "command:add",
        "command:add",
        "command:half",
        "command:flag",
    ]
    assert Typed.errors[0][1].usage == "!add <x> [y]"
    assert [s for s in Typed.seen if s[0] == "add"].count(("add", 7)) == 1
    # Extra words are ignored, not errors: ctx.args keeps everything.
    run(gw._emit("MESSAGE_CREATE", guild_msg("!add 1 2 3")))
    assert ("add", 3) in Typed.seen
    assert len(Typed.errors) == 4
    run(c.close())


def test_remainder_and_var_positional(monkeypatch):
    Typed.seen.clear()
    Typed.errors.clear()
    c, gw = load_typed(monkeypatch)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!say hello brave world")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!sum 1 2 3")))
    assert ("say", "hello brave world") in Typed.seen
    assert ("sum", 6) in Typed.seen
    run(gw._emit("MESSAGE_CREATE", guild_msg("!say")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!sum 1 x")))
    assert len(Typed.errors) == 2
    assert all(isinstance(exc, UsageError) for _, exc in Typed.errors)
    run(c.close())


def test_entity_resolution(monkeypatch):
    Typed.seen.clear()
    Typed.errors.clear()
    c, gw = load_typed(monkeypatch)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!who <@u2>")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!who Zeddy")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!who zed")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!u <@u3>")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!u u3")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!ch <#c9>")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!ch general")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!ch #general")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!role r1")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!role Mods")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!role <@&r1>")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!srv g1")))
    assert Typed.seen.count(("who", "u2")) == 3
    assert Typed.seen.count(("u", "u3")) == 2
    assert Typed.seen.count(("ch", "c9")) == 3
    assert Typed.seen.count(("role", "r1")) == 3
    assert ("srv", "g1") in Typed.seen
    assert Typed.errors == []

    run(gw._emit("MESSAGE_CREATE", guild_msg("!who stranger")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!u u9")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!ch nowhere")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!role peasant")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!srv xyz")))
    assert len(Typed.errors) == 5
    assert all(isinstance(exc, UsageError) for _, exc in Typed.errors)
    run(c.close())


def test_message_resolution(monkeypatch):
    Typed.seen.clear()
    Typed.errors.clear()
    c, gw = load_typed(monkeypatch)

    class MsgMod(Module):
        @command("msg")
        async def msg(self, ctx, message: Message):
            Typed.seen.append(("msg", message.id))

    async def fake_request(method, path, **kwargs):
        assert method == "GET"
        if path == "/channels/c1/messages/m9":
            return {"id": "m9", "channel_id": "c1", "content": "found"}
        raise NotFound("missing", status=404, method=method, path=path)

    monkeypatch.setattr(c.http, "request", fake_request)
    c.modules.load(MsgMod)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!msg m9")))
    assert ("msg", "m9") in Typed.seen
    run(gw._emit("MESSAGE_CREATE", msg_payload("!msg nope")))
    assert len(Typed.errors) == 1
    assert isinstance(Typed.errors[0][1], UsageError)
    run(c.close())


def test_bad_signatures_fail_at_load(monkeypatch):
    c, gw = patched(monkeypatch)

    class Weird(Module):
        @command("weird")
        async def weird(self, ctx, when: dict):
            pass  # pragma: no cover

    class Kwargs(Module):
        @command("kw")
        async def kw(self, ctx, **rest):
            pass  # pragma: no cover

    class TwoRemainders(Module):
        @command("two")
        async def two(self, ctx, *, first: str, second: str = ""):
            pass  # pragma: no cover

    with pytest.raises(ValueError, match="unsupported type"):
        c.modules.load(Weird)
    with pytest.raises(ValueError, match=r"\*\*kwargs"):
        c.modules.load(Kwargs)
    with pytest.raises(ValueError, match="keyword-only"):
        c.modules.load(TwoRemainders)
    run(c.close())


def test_string_annotations_from_future_import(monkeypatch, tmp_path):
    c, gw = patched(monkeypatch)
    c.on_error(lambda event, exc: Typed.errors.append((event, exc)))
    Typed.seen.clear()
    Typed.errors.clear()
    mod_file = tmp_path / "futuremod.py"
    mod_file.write_text(
        "from __future__ import annotations\n"
        "from despise.cmd import Module, command\n"
        "from despise.models import Member\n"
        "\n"
        "class FutureMod(Module):\n"
        "    seen = []\n"
        "\n"
        "    @command('fwho')\n"
        "    async def fwho(self, ctx, member: Member):\n"
        "        FutureMod.seen.append(('fwho', member.id))\n"
    )
    c.cache.put_member(
        Member({"user": {"id": "u2", "username": "Zed"}, "guild_id": "g1", "nick": "Zeddy"})
    )
    c.modules.load(str(mod_file))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!fwho <@u2>")))
    assert c.modules.get("futuremod").seen == [("fwho", "u2")]
    assert Typed.errors == []
    run(c.close())


def test_command_without_ctx(monkeypatch):
    Typed.seen.clear()
    Typed.errors.clear()
    c, gw = load_typed(monkeypatch)

    class NoCtx(Module):
        @command("raw")
        async def raw(self, target: Member):
            Typed.seen.append(("raw", target.id))

    c.modules.load(NoCtx)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!raw <@u2>")))
    assert ("raw", "u2") in Typed.seen
    assert Typed.errors == []
    run(c.close())


async def _yes(ctx):
    return True


class Gated(Module):
    name = "gatedmod"
    seen = []
    errors = []

    @command("open")
    async def open(self, ctx):
        Gated.seen.append("open")

    @command("shh")
    @require(lambda ctx: False)
    async def shh(self, ctx):
        Gated.seen.append("shh")

    @command("stacked")
    @require(lambda ctx: True)
    @require(_yes)
    async def stacked(self, ctx):
        Gated.seen.append("stacked")

    @command("typed")
    @require(lambda ctx: False)
    async def typed(self, ctx, target: Member):
        Gated.seen.append(("typed", target.id))


def load_gated(monkeypatch):
    c, gw = patched(monkeypatch)
    c.modules.load(Gated)
    c.on_error(lambda event, exc: Gated.errors.append((event, exc)))

    async def _offline(*args, **kwargs):
        raise DespiseError("offline")

    monkeypatch.setattr(c.http, "request", _offline)
    return c, gw


def test_checks_pass_and_block(monkeypatch):
    Gated.seen.clear()
    Gated.errors.clear()
    c, gw = load_gated(monkeypatch)
    run(gw._emit("MESSAGE_CREATE", msg_payload("!open")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!stacked")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!shh")))
    assert "open" in Gated.seen
    assert "stacked" in Gated.seen
    assert "shh" not in Gated.seen
    assert len(Gated.errors) == 1
    event, exc = Gated.errors[0]
    assert event == "command:shh"
    assert isinstance(exc, CheckFailure)
    assert isinstance(exc, UsageError)
    assert exc.usage == "!shh"
    assert "blocked this command" in str(exc)
    run(c.close())


def test_checks_run_before_conversion(monkeypatch):
    # An unresolvable member would be a UsageError (plus a REST attempt)
    # if conversion ran first; the check must win instead.
    Gated.seen.clear()
    Gated.errors.clear()
    c, gw = load_gated(monkeypatch)
    run(gw._emit("MESSAGE_CREATE", guild_msg("!typed nope-not-a-member")))
    assert [s for s in Gated.seen if isinstance(s, tuple)] == []
    assert len(Gated.errors) == 1
    assert isinstance(Gated.errors[0][1], CheckFailure)
    run(c.close())


def test_builtin_guild_dm_channel(monkeypatch):
    seen = []

    class Zones(Module):
        @command("gonly")
        @require(in_guild())
        async def gonly(self, ctx):
            seen.append("gonly")

        @command("dmonly")
        @require(in_dm())
        async def dmonly(self, ctx):
            seen.append("dmonly")

        @command("here")
        @require(in_channel("c1"))
        async def here(self, ctx):
            seen.append("here")

    c, gw = patched(monkeypatch)
    c.modules.load(Zones)
    c.on_error(lambda event, exc: seen.append(("err", event)))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!gonly")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!gonly")))
    run(gw._emit("MESSAGE_CREATE", msg_payload("!dmonly")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!dmonly")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!here")))
    other = guild_msg("!here")
    other["channel_id"] = "c2"
    run(gw._emit("MESSAGE_CREATE", other))
    assert seen.count("gonly") == 1
    assert seen.count("dmonly") == 1
    assert seen.count("here") == 1
    assert [e for e in seen if isinstance(e, tuple)] == [
        ("err", "command:gonly"),
        ("err", "command:dmonly"),
        ("err", "command:here"),
    ]
    run(c.close())


def test_builtin_roles_and_owner(monkeypatch):
    seen = []
    errors = []

    class Priv(Module):
        @command("mod")
        @require(has_role("r1"))
        async def mod(self, ctx):
            seen.append("mod")

        @command("mine")
        @require(is_owner())
        async def mine(self, ctx):
            seen.append("mine")

    c, gw = patched(monkeypatch)
    c.modules.load(Priv)
    c.on_error(lambda event, exc: errors.append((event, exc)))
    with_role = guild_msg("!mod")
    with_role["member"] = {
        "user": {"id": "u1", "username": "tester"},
        "guild_id": "g1",
        "roles": ["r1"],
    }
    run(gw._emit("MESSAGE_CREATE", with_role))
    c.cache.put_member(
        Member(
            {
                "user": {"id": "u9", "username": "cached"},
                "guild_id": "g1",
                "roles": ["r1"],
            }
        )
    )
    run(gw._emit("MESSAGE_CREATE", msg_payload("!mod", author_id="u9", guild_id="g1")))
    # u7 has no roles anywhere: member missing, cache empty.
    run(gw._emit("MESSAGE_CREATE", msg_payload("!mod", author_id="u7", guild_id="g1")))
    run(gw._emit("MESSAGE_CREATE", guild_msg("!mine")))
    c.owner_ids = {"u1"}
    run(gw._emit("MESSAGE_CREATE", guild_msg("!mine")))
    assert seen == ["mod", "mod", "mine"]
    assert [event for event, _ in errors] == ["command:mod", "command:mine"]
    assert all(isinstance(exc, CheckFailure) for _, exc in errors)
    run(c.close())


def test_require_on_listener_fails_at_load(monkeypatch):
    class Bad(Module):
        @listen("message")
        @require(lambda ctx: True)
        async def ears(self, msg):
            pass  # pragma: no cover

    c, gw = patched(monkeypatch)
    with pytest.raises(ValueError):
        c.modules.load(Bad)
    run(c.close())


def test_require_needs_a_callable():
    with pytest.raises(TypeError):
        require(42)
