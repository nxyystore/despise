"""Minimal background task helper (``ext.tasks``, minus the extension).

A deliberate omission from the rest of the surface: this is just enough to
run a coroutine on an interval without a user writing their own task
bookkeeping. It is not a scheduler and has no cron syntax.

    @task_loop(seconds=30)
    async def announce():
        await bot.rest.send_message(CHANNEL, "tick")

    bot.start_task(announce)   # registers it so Client.close() cancels it

The wrapped coroutine keeps its normal signature; the loop object is
available as the ``this`` argument if you need it, mirroring discord.py.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger("despise.tasks")


class Task:
    """One scheduled invocation of a coroutine function."""

    def __init__(
        self,
        coro: Callable[..., Coroutine[Any, Any, Any]],
        *,
        seconds: float = 0.0,
        minutes: float = 0.0,
        hours: float = 0.0,
        jitter: float = 0.0,
        max_runs: int | None = None,
        name: str | None = None,
    ) -> None:
        total = seconds + minutes * 60 + hours * 3600
        if total <= 0:
            raise ValueError("a task needs a positive interval")
        if jitter < 0 or jitter > 1:
            raise ValueError("jitter must be between 0 and 1")
        self.coro = coro
        self.interval = total
        self.jitter = jitter
        self.max_runs = max_runs
        self.name = name or getattr(coro, "__name__", "task")
        self._task: asyncio.Task | None = None
        self._runs = 0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def running(self) -> bool:
        """Alias of ``is_running`` (matches TypingLoop)."""
        return self.is_running

    @property
    def runs(self) -> int:
        return self._runs

    def start(self) -> None:
        if self.is_running:
            log.debug("task %s already running", self.name)
            return
        self._runs = 0
        self._task = asyncio.create_task(self._loop(), name=f"despise-task-{self.name}")

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    # ``stop`` reads better at call sites; keep both.
    stop = cancel

    async def _sleep_interval(self) -> None:
        delay = self.interval
        if self.jitter:
            # +/- jitter fraction of the interval
            spread = delay * self.jitter
            delay = max(0.0, delay + random.uniform(-spread, spread))
        await asyncio.sleep(delay)

    async def _loop(self) -> None:
        try:
            while True:
                await self._sleep_interval()
                # Wait on the callback *and* the stop signal so cancel()
                # does not have to wait out a full interval.
                call = asyncio.ensure_future(self._invoke())
                try:
                    await call
                except asyncio.CancelledError:
                    call.cancel()
                    raise
                self._runs += 1
                if self.max_runs is not None and self._runs >= self.max_runs:
                    log.debug("task %s reached max_runs", self.name)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A task that dies silently is worse than a noisy one.
            log.exception("task %s crashed", self.name)
        finally:
            self._task = None

    async def _invoke(self) -> None:
        try:
            result = self.coro(self)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad tick must not kill the schedule.
            log.exception("task %s raised during a run", self.name)

    def __repr__(self) -> str:  # pragma: no cover
        state = "running" if self.is_running else "stopped"
        return f"<Task {self.name} every {self.interval}s {state}>"


def loop(
    *,
    seconds: float = 0.0,
    minutes: float = 0.0,
    hours: float = 0.0,
    jitter: float = 0.0,
    max_runs: int | None = None,
    name: str | None = None,
) -> Callable[[Callable[..., Coroutine[Any, Any, Any]]], Task]:
    """Decorator turning a coroutine function into a :class:`Task`."""

    def decorator(coro: Callable[..., Coroutine[Any, Any, Any]]) -> Task:
        return Task(
            coro,
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            jitter=jitter,
            max_runs=max_runs,
            name=name,
        )

    return decorator


__all__ = ["Task", "loop"]
