"""Typed command arguments: ``async def ban(self, ctx, member: Member)``.

Parameters after ``ctx`` fill from the message words in order, converted
by annotation. Unannotated means ``str``. Failures raise ``UsageError``
(which carries a ``!name <arg>`` usage string) instead of tracebacks.

Supported annotations: ``str`` ``int`` ``float`` ``bool`` ``User``
``Member`` ``Channel`` ``Role`` ``Guild`` ``Message``. ``*args`` collects
leftovers (converting each by its annotation); one keyword-only
parameter takes the whole remainder as a single string, so
``async def say(self, ctx, *, text: str)`` answers ``!say anything here``.
"""

from __future__ import annotations

import inspect
import math
import re
import typing
from typing import Any

from ..exceptions import DespiseError, Forbidden, NotFound
from ..models import Channel, Guild, Member, Message, Role, User
from .errors import UsageError
from .module import Context

_INT_RE = re.compile(r"^[+-]?\d+$")
_USER_MENTION_RE = re.compile(r"<@!?([^<>\s]+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#([^<>\s]+)>")
_ROLE_MENTION_RE = re.compile(r"<@&([^<>\s]+)>")

_TRUE_WORDS = {"1", "true", "t", "yes", "y", "on", "enable", "enabled"}
_FALSE_WORDS = {"0", "false", "f", "no", "n", "off", "disable", "disabled"}

_MISSING = inspect.Parameter.empty

#: Annotations converters understand, besides "no annotation means str".
SUPPORTED = (str, int, float, bool, User, Member, Channel, Role, Guild, Message, Context)


def _is_ctx_param(param: inspect.Parameter) -> bool:
    return (
        param.name == "ctx"
        or param.annotation in (Context, "Context", _MISSING, None)
    ) and param.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def _split_params(fn) -> tuple[bool, list[inspect.Parameter]]:
    """Drop the leading ``ctx`` (when present); return ``(pass_ctx, rest)``."""
    params = list(inspect.signature(fn).parameters.values())
    if params and _is_ctx_param(params[0]):
        return True, params[1:]
    return False, params


def _types(fn, params: list[inspect.Parameter]) -> dict[str, Any]:
    """Resolve annotations (including string ones from future imports)."""
    target = inspect.unwrap(fn)
    func = target.__func__ if inspect.ismethod(target) else target
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}
    return {p.name: hints.get(p.name, p.annotation) for p in params}


def _unwrap(annotation):
    """Accept ``T`` or ``T | None``; return ``T`` or ``None`` if unsupported."""
    if annotation in (_MISSING, None):
        return str
    if annotation in SUPPORTED:
        return annotation
    if isinstance(annotation, str):
        return None
    args = typing.get_args(annotation)
    if len(args) == 2 and type(None) in args:
        inner = args[0] if args[1] is type(None) else args[1]
        if inner in SUPPORTED:
            return inner
    return None


def validate_command(names, fn) -> None:
    """Reject unsupported signatures at load, not at first invocation."""
    _, params = _split_params(fn)
    resolved = _types(fn, params)
    shown = ",".join(names)
    kinds = [p.kind for p in params]
    if inspect.Parameter.VAR_KEYWORD in kinds:
        raise ValueError(f"command {shown!r} takes **kwargs, which commands do not support")
    kwonly = [p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY]
    if len(kwonly) > 1:
        raise ValueError(
            f"command {shown!r} takes {len(kwonly)} keyword-only parameters; "
            "at most one (the remainder) is allowed"
        )
    for p in params:
        ann = resolved[p.name]
        if isinstance(ann, str):
            raise ValueError(
                f"command {shown!r} parameter {p.name!r} has annotation "
                f"{ann!r} that does not resolve; import the type"
            )
        if _unwrap(ann) is None:
            raise ValueError(
                f"command {shown!r} parameter {p.name!r} has unsupported type "
                f"{getattr(ann, '__name__', ann)!r}"
            )


def usage_for(invoked: str, fn, *, prefix: str = "!") -> str:
    """Render ``!ban <member> [reason] <rest...>`` from the signature."""
    _, params = _split_params(fn)
    parts = []
    for p in params:
        if p.kind is inspect.Parameter.VAR_POSITIONAL or p.kind is inspect.Parameter.KEYWORD_ONLY:
            parts.append(f"<{p.name}...>")
        elif p.default is _MISSING:
            parts.append(f"<{p.name}>")
        else:
            parts.append(f"[{p.name}]")
    head = f"{prefix}{invoked}"
    return f"{head} " + " ".join(parts) if parts else head


def _convert_scalar(annotation, raw: str, usage: str):
    if annotation in (_MISSING, None, str):
        return raw
    if annotation is bool:
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise UsageError(f"{raw!r} is not yes/no", usage=usage)
    if annotation is int:
        if _INT_RE.match(raw.strip()):
            return int(raw.strip())
        raise UsageError(f"{raw!r} is not a whole number", usage=usage)
    if annotation is float:
        try:
            value = float(raw.strip())
        except ValueError:
            raise UsageError(f"{raw!r} is not a number", usage=usage) from None
        if not math.isfinite(value):
            raise UsageError(f"{raw!r} is not a finite number", usage=usage)
        return value
    raise AssertionError(f"unreachable scalar {annotation!r}")  # pragma: no cover


def _mention_or_id(pattern: re.Pattern, raw: str) -> str | None:
    match = pattern.fullmatch(raw.strip())
    if match:
        return match.group(1)
    if _INT_RE.match(raw.strip()):
        return raw.strip()
    return None


def _names_equal(want: str, *have: Any) -> bool:
    folded = want.casefold()
    return any(isinstance(h, str) and h.casefold() == folded for h in have if h)


async def _safe_lookup(coro):
    """Await a Client/REST lookup, treating transport failures as not found."""
    try:
        return await coro
    except DespiseError:
        return None


async def _convert_entity(bot: Any, ctx: Context, annotation, raw: str, usage: str):
    message = ctx.message
    guild_id = str(getattr(message, "guild_id", "") or "")
    channel_id = str(getattr(message, "channel_id", "") or "")

    if annotation is User:
        uid = _mention_or_id(_USER_MENTION_RE, raw) or raw.strip()
        user = await _safe_lookup(bot.get_user(uid)) if uid else None
        if user is None:
            raise UsageError(f"no user matching {raw!r}", usage=usage)
        return user

    if annotation is Guild:
        guild = await _safe_lookup(bot.get_guild(raw.strip())) if raw.strip() else None
        if guild is None:
            raise UsageError(f"no server matching {raw!r}", usage=usage)
        return guild

    if annotation is Message:
        found = None
        if raw.strip() and channel_id:
            try:
                data = await bot.http.request(
                    "GET", f"/channels/{channel_id}/messages/{raw.strip()}"
                )
            except (NotFound, Forbidden, DespiseError):
                data = None
            if isinstance(data, dict):
                found = Message(data)
        if found is None:
            raise UsageError(f"no message matching {raw!r} here", usage=usage)
        return found

    if not guild_id:
        need = {Member: "members", Channel: "channels", Role: "roles"}[annotation]
        raise UsageError(f"{need} need a server; this is not one", usage=usage)

    if annotation is Member:
        member_id = _mention_or_id(_USER_MENTION_RE, raw)
        if member_id:
            member = bot.get_member(member_id, guild_id)
            if member is not None:
                return member
        guild = await _safe_lookup(bot.get_guild(guild_id))
        if guild is not None:
            member = bot.get_member(raw.strip(), guild_id)
            if member is not None:
                return member
            for member in getattr(guild, "members", []) or []:
                user = getattr(member, "user", None)
                if _names_equal(
                    raw.strip(),
                    getattr(member, "nick", None),
                    getattr(member, "display_name", None),
                    getattr(user, "username", None),
                    getattr(user, "display_name", None),
                ):
                    return member
        raise UsageError(f"no member matching {raw!r}", usage=usage)

    if annotation is Channel:
        cid = _mention_or_id(_CHANNEL_MENTION_RE, raw)
        if cid:
            channel = await _safe_lookup(bot.get_channel(cid))
            if channel is not None:
                return channel
        elif raw.strip().isdigit():
            channel = await _safe_lookup(bot.get_channel(raw.strip()))
            if channel is not None:
                return channel
        else:
            cache = getattr(bot, "cache", None)
            cached = cache.get_channel(raw.strip()) if cache is not None else None
            if cached is not None:
                return cached._bind(bot)
        want = raw.strip().removeprefix("#")
        guild = await _safe_lookup(bot.get_guild(guild_id))
        for channel in getattr(guild, "channels", []) or []:
            if _names_equal(want, getattr(channel, "name", None)):
                return channel
        raise UsageError(f"no channel matching {raw!r}", usage=usage)

    if annotation is Role:
        guild = await _safe_lookup(bot.get_guild(guild_id))
        roles = getattr(guild, "roles", []) or []
        want_id = _mention_or_id(_ROLE_MENTION_RE, raw) or raw.strip()
        for role in roles:
            if str(getattr(role, "id", "")) == want_id:
                return role
        for role in roles:
            if _names_equal(raw.strip(), getattr(role, "name", None)):
                return role
        raise UsageError(f"no role matching {raw!r}", usage=usage)

    raise AssertionError(f"unreachable entity {annotation!r}")  # pragma: no cover


async def _convert_one(bot: Any, ctx: Context, annotation, raw: str, usage: str):
    if annotation in (_MISSING, None, str, bool, int, float):
        return _convert_scalar(annotation, raw, usage)
    return await _convert_entity(bot, ctx, annotation, raw, usage)


async def bind_command(fn, invoked: str, ctx: Context, args: list[str]):
    """Map message words onto ``fn``'s parameters.

    Returns ``(pass_ctx, call_args, call_kwargs)``. Raises ``UsageError``
    (with ``.usage`` set) for missing, extra, or unconvertible arguments.
    """
    prefix = getattr(getattr(ctx, "bot", None), "command_prefix", "!")
    usage = usage_for(invoked, fn, prefix=prefix)
    pass_ctx, params = _split_params(fn)
    resolved = {pname: _unwrap(t) for pname, t in _types(fn, params).items()}
    positional = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    var = next(
        (p for p in params if p.kind is inspect.Parameter.VAR_POSITIONAL), None
    )
    kwonly = next(
        (p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY), None
    )

    call_args: list = []
    call_kwargs: dict = {}
    take = 0
    for p in positional:
        if take < len(args):
            call_args.append(await _convert_one(ctx.bot, ctx, resolved[p.name], args[take], usage))
            take += 1
        elif p.default is not _MISSING:
            call_args.append(p.default)
        else:
            raise UsageError(f"missing <{p.name}>", usage=usage)
    leftover = args[take:]
    if var is not None:
        for raw in leftover:
            call_args.append(await _convert_one(ctx.bot, ctx, resolved[var.name], raw, usage))
        leftover = []
    if kwonly is not None:
        if leftover:
            call_kwargs[kwonly.name] = await _convert_one(
                ctx.bot, ctx, resolved[kwonly.name], " ".join(leftover), usage
            )
        elif kwonly.default is not _MISSING:
            call_kwargs[kwonly.name] = kwonly.default
        else:
            raise UsageError(f"missing <{kwonly.name}...>", usage=usage)
    # Anything left over stays available as ctx.args; annotated commands
    # convert the leading words and ignore the rest.
    return pass_ctx, call_args, call_kwargs
