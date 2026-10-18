"""Loading, unloading, and reloading modules at runtime."""

from __future__ import annotations

import importlib
import inspect
import logging
import shlex
from typing import Any

from .convert import bind_command, usage_for, validate_command
from .errors import CheckFailure, UsageError
from .module import Context, Module

log = logging.getLogger("despise.cmd")


async def _check_gate(fn: Any, invoked: str, ctx: Context) -> None:
    """Run a command's @require predicates. Raises CheckFailure."""
    for pred in getattr(fn, "_checks", ()):
        result = pred(ctx)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            name = getattr(pred, "__name__", "check")
            raise CheckFailure(
                f"{name} blocked this command",
                usage=usage_for(invoked, fn),
                predicate=pred,
            )


class ModuleManager:
    """Owns every loaded module: ``bot.modules.load("cmd.general")``.

    A module loads from an instance, a ``Module`` subclass, or a dotted
    path to a file under ``cmd/`` (or anywhere importable). Unloading
    unhooks its listeners and forgets its commands. Reloading only
    works for path-loaded modules: it unloads, re-imports, and loads.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.loaded: dict[str, Module] = {}
        self._sources: dict[str, str] = {}  # key -> dotted path, for reload
        self._hooks: dict[str, list[tuple[str, Any]]] = {}  # key -> listeners
        self._commands: dict[str, tuple[str, Any]] = {}  # name -> (key, fn)
        self._dispatcher_on = False

    # -- loading ------------------------------------------------------
    def load(self, target: Module | type[Module] | str) -> Module:
        """Load a module and return the instance."""
        if isinstance(target, str):
            mod = self._load_path(target)
            self._sources[mod.module_name] = target
            return mod
        if isinstance(target, type):
            if not (isinstance(target, type) and issubclass(target, Module)):
                raise TypeError(f"cannot load {target!r}: not a Module subclass")
            return self._register(target(self.bot))
        if isinstance(target, Module):
            return self._register(target)
        raise TypeError(f"cannot load {target!r}: pass an instance, class, or path")

    def _load_path(self, path: str) -> Module:
        if path.endswith(".py") or "/" in path or "\\" in path:
            imported = self._import_file(path)
            label = path
        else:
            dotted = path
            try:
                imported = importlib.import_module(dotted)
            except ImportError as exc:
                raise ImportError(f"cannot load module {path!r}: {exc}") from exc
            label = dotted
        found = [
            value(self.bot)
            for value in vars(imported).values()
            if (
                isinstance(value, type)
                and issubclass(value, Module)
                and value is not Module
                and value.__module__ == imported.__name__
            )
        ]
        if not found:
            raise ImportError(f"module {label!r} defines no Module subclass")
        if len(found) > 1:
            log.warning("module %r defines %d Modules; loading all", label, len(found))
        for module in found[1:]:
            self._register(module)
        return self._register(found[0])

    @staticmethod
    def _import_file(path: str):
        """Exec a module file fresh from disk. Never cached, so reload
        always picks up edits, and plain folders (like ``cmd/``) work
        without shadowing same-named stdlib packages."""
        import sys
        from importlib.util import module_from_spec, spec_from_file_location
        from pathlib import Path

        file = Path(path)
        if not file.is_file():
            raise ImportError(f"cannot load module {path!r}: file not found")
        name = f"despise_cmd_{file.stem}"
        spec = spec_from_file_location(name, file)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load module {path!r}: bad spec")
        imported = module_from_spec(spec)
        sys.modules[name] = imported
        try:
            spec.loader.exec_module(imported)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return imported

    def _register(self, module: Module) -> Module:
        key = module.module_name
        if key in self.loaded:
            raise ValueError(f"module {key!r} is already loaded")
        commands = list(module.iter_commands())
        command_ids = {id(fn) for _, fn in commands}
        hooks: list[tuple[str, Any]] = []
        for event, fn in module.iter_listeners():
            if getattr(fn, "_checks", None) and id(fn) not in command_ids:
                raise ValueError(
                    f"@{event} listener in {key!r} has @require; "
                    "checks only work on commands"
                )
            self.bot.on(event, fn)
            hooks.append((event, fn))
        if commands:
            self._ensure_dispatcher()
            for names, fn in commands:
                validate_command(names, fn)
                for name in names:
                    name = name.lower()
                    if name in self._commands:
                        owner = self._commands[name][0]
                        raise ValueError(
                            f"command {name!r} from {key!r} "
                            f"is already owned by {owner!r}"
                        )
                    self._commands[name] = (key, fn)
        self.loaded[key] = module
        self._hooks[key] = hooks
        log.info("loaded module %r", key)
        return module

    # -- unloading ----------------------------------------------------
    def unload(self, name: str) -> Module:
        """Unload by module name (or dotted path). Returns the instance."""
        key = name
        self._sources.pop(name, None)
        try:
            module = self.loaded.pop(key)
        except KeyError:
            raise KeyError(f"no loaded module {name!r}") from None
        for _event, fn in self._hooks.pop(key, []):
            self.bot._unhook(fn)
        dead = [n for n, (owner, _) in self._commands.items() if owner == key]
        for n in dead:
            del self._commands[n]
        log.info("unloaded module %r", key)
        return module

    def reload(self, name: str) -> Module:
        """Unload and re-import a path-loaded module.

        Files exec fresh from disk, so edits apply. Dotted paths drop
        from ``sys.modules`` first for the same reason. Instance-loaded
        modules cannot reload: unload them and load a fresh instance.
        """
        import sys

        path = self._sources.get(name)
        if path is None:
            if "." in name or "/" in name or "\\" in name:
                path = name
            else:
                raise ValueError(
                    f"cannot reload {name!r}: only path-loaded modules reload; "
                    "unload it and load a fresh instance instead"
                )
        import contextlib

        with contextlib.suppress(KeyError):
            self.unload(name)
        if not (path.endswith(".py") or "/" in path or "\\" in path):
            sys.modules.pop(path, None)
        importlib.invalidate_caches()
        return self.load(path)

    def get(self, name: str) -> Module | None:
        return self.loaded.get(name)

    # -- dispatch -----------------------------------------------------
    def _ensure_dispatcher(self) -> None:
        if self._dispatcher_on:
            return
        self._dispatcher_on = True
        self.bot.on("message", self._dispatch)

    async def _dispatch(self, message: Any) -> None:
        content = getattr(message, "content", "") or ""
        if not isinstance(content, str):
            return
        prefix = self.bot.command_prefix
        if not content.startswith(prefix):
            return
        rest = content[len(prefix):].strip()
        if not rest:
            return
        try:
            parts = shlex.split(rest)
        except ValueError:
            parts = rest.split()
        if not parts:
            return
        name = parts[0].lower()
        entry = self._commands.get(name)
        if entry is None:
            return
        _, fn = entry
        ctx = Context(self.bot, message, name, parts[1:])
        try:
            await _check_gate(fn, name, ctx)
            pass_ctx, call_args, call_kwargs = await bind_command(
                fn, name, ctx, parts[1:]
            )
        except UsageError as exc:
            await self.bot._dispatch_error(f"command:{name}", exc)
            return
        try:
            if pass_ctx:
                await fn(ctx, *call_args, **call_kwargs)
            else:
                await fn(*call_args, **call_kwargs)
        except Exception as exc:
            await self.bot._dispatch_error(f"command:{name}", exc)
