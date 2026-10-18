"""Modules: one class, some commands, some listeners.

A module groups prefix commands (``!ping``) and event listeners in a
single class. The loader in ``loader.py`` wires them into the client;
this file only declares what a module *is*.
"""

from __future__ import annotations

from typing import Any


class Context:
    """What a command handler receives.

    ``message`` is the ``Message`` model, ``args`` the words after the
    command name, ``text`` those words rejoined. ``reply`` answers in
    the same channel.
    """

    def __init__(self, bot: Any, message: Any, name: str, args: list[str]) -> None:
        self.bot = bot
        self.message = message
        self.name = name
        self.args = args

    @property
    def text(self) -> str:
        return " ".join(self.args)

    @property
    def author(self) -> Any:
        return self.message.author

    @property
    def channel_id(self) -> str:
        return self.message.channel_id

    async def reply(self, content: str, **kwargs: Any) -> Any:
        return await self.bot.rest.send_message(self.channel_id, content, **kwargs)

    async def send(self, content: str, **kwargs: Any) -> Any:
        return await self.reply(content, **kwargs)


def command(name: str | None = None, *, aliases: list[str] | tuple[str, ...] = ()):
    """Mark a method as a prefix command: ``!name args...``.

    The method takes ``(self, ctx)``. ``name`` defaults to the method
    name with underscores kept (``server_info`` answers ``!server_info``).
    Extra triggers go in ``aliases=["p", "pong"]``.

    Parameters after ``ctx`` fill from the message words by annotation
    (``member: Member``, ``count: int = 1``). ``*args`` collects
    leftovers; one keyword-only parameter takes the whole remainder.
    The first parameter is ``ctx``, unless it carries a converter
    annotation (``Member``, ``int``, ...) -- then the command takes
    its arguments directly and receives no ``ctx``.
    """

    def deco(fn):
        fn._cmd_names = (name or fn.__name__,) + tuple(aliases)
        return fn

    return deco


def listen(event: str):
    """Mark a method as an event listener.

    ``event`` takes the same friendly or raw names as ``Client.on``,
    and the method receives what such a listener receives.
    """

    def deco(fn):
        fn._listens_to = event
        return fn

    return deco


def require(predicate):
    """Gate a command behind a check: ``@require(in_guild())``.

    ``predicate`` takes ``ctx`` and returns truthy to allow the command.
    Sync or async both work. Stack several; they run bottom-up,
    nearest the function first. A failure raises
    ``CheckFailure`` on the ``command:name`` error channel, before any
    argument conversion runs.

    Only meaningful on @command methods; putting it on a @listen
    method fails the load.
    """
    if not callable(predicate):
        raise TypeError(f"@require needs a callable, not {predicate!r}")

    def deco(fn):
        checks = getattr(fn, "_checks", ())
        fn._checks = tuple(checks) + (predicate,)
        return fn

    return deco


class Module:
    """Base class for a loadable module. Subclass it, set ``name``."""

    name: str | None = None

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @property
    def module_name(self) -> str:
        return self.name or type(self).__name__.lower()

    def iter_commands(self):
        """Yield ``(names, bound_method)`` for each @command method."""
        seen: set[int] = set()
        for attr in dir(self):
            if attr.startswith("__"):
                continue
            fn = getattr(self, attr)
            names = getattr(fn, "_cmd_names", None)
            if names is not None and id(fn) not in seen:
                seen.add(id(fn))
                yield names, fn

    def iter_listeners(self):
        """Yield ``(event, bound_method)`` for each @listen method."""
        seen: set[int] = set()
        for attr in dir(self):
            if attr.startswith("__"):
                continue
            fn = getattr(self, attr)
            event = getattr(fn, "_listens_to", None)
            if event is not None and id(fn) not in seen:
                seen.add(id(fn))
                yield event, fn
