"""Task helper: scheduling, jitter, max_runs, crash isolation, cancellation."""

import asyncio

import pytest

from despise.tasks import Task, loop


def run(coro):
    return asyncio.run(coro)


def test_interval_requires_a_positive_value():
    with pytest.raises(ValueError, match="positive interval"):
        Task(lambda this: None)


def test_jitter_bounds_are_validated():
    with pytest.raises(ValueError, match="jitter"):
        Task(lambda this: None, seconds=1, jitter=2)


def test_interval_converts_minutes_and_hours():
    assert Task(lambda this: None, seconds=30).interval == 30
    assert Task(lambda this: None, minutes=2).interval == 120
    assert Task(lambda this: None, hours=1, minutes=1, seconds=30).interval == 3690


def test_loop_decorator_returns_a_task():
    @loop(seconds=5)
    async def ticker(this):
        pass

    assert isinstance(ticker, Task)
    assert ticker.interval == 5
    assert ticker.name == "ticker"


def test_task_runs_repeatedly():
    ticks = []

    @loop(seconds=0.01, max_runs=3)
    async def ticker(this):
        ticks.append(this)

    async def main():
        ticker.start()
        assert ticker.running
        for _ in range(200):
            if len(ticks) >= 3:
                break
            await asyncio.sleep(0.005)
        return len(ticks)

    assert run(main()) == 3
    assert ticker.runs == 3


def test_task_receives_itself():
    seen = []

    @loop(seconds=0.01, max_runs=1)
    async def ticker(this):
        seen.append(this)

    async def main():
        ticker.start()
        await asyncio.sleep(0.1)

    run(main())
    assert seen == [ticker]


def test_a_raising_run_does_not_kill_the_schedule():
    ticks = []

    @loop(seconds=0.01, max_runs=3)
    async def flaky(this):
        ticks.append(1)
        raise RuntimeError("boom")

    async def main():
        flaky.start()
        for _ in range(200):
            if len(ticks) >= 3:
                break
            await asyncio.sleep(0.005)

    run(main())
    assert len(ticks) == 3


def test_start_is_idempotent():
    @loop(seconds=0.01)
    async def ticker(this):
        pass

    async def main():
        ticker.start()
        first = ticker._task
        ticker.start()
        assert ticker._task is first

    run(main())
    ticker.cancel()


def test_cancel_stops_the_task():
    @loop(seconds=0.01)
    async def ticker(this):
        pass

    async def main():
        ticker.start()
        await asyncio.sleep(0.03)
        ticker.cancel()
        await asyncio.sleep(0.02)
        return ticker.is_running

    assert run(main()) is False


def test_stop_is_an_alias_for_cancel():
    @loop(seconds=0.01)
    async def ticker(this):
        pass

    async def main():
        ticker.start()
        await asyncio.sleep(0.02)
        ticker.stop()
        return ticker.is_running

    assert run(main()) is False


def test_jitter_perturbs_the_delay():
    task = Task(lambda this: None, seconds=1.0, jitter=0.5)
    delays = []
    original = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)

    async def main():
        asyncio.sleep = fake_sleep
        try:
            await task._sleep_interval()
            await task._sleep_interval()
            await task._sleep_interval()
        finally:
            asyncio.sleep = original

    run(main())
    assert len(delays) == 3
    assert any(d != delays[0] for d in delays), "jitter produced no variation"
    assert all(0.5 <= d <= 1.5 for d in delays)


def test_zero_jitter_is_deterministic():
    task = Task(lambda this: None, seconds=1.0)
    delays = []
    original = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)

    async def main():
        asyncio.sleep = fake_sleep
        try:
            await task._sleep_interval()
        finally:
            asyncio.sleep = original

    run(main())
    assert delays == [1.0]


def test_sync_callbacks_are_awaited_safely():
    calls = []
    task = Task(lambda this: calls.append(1), seconds=1)

    async def main():
        await task._invoke()

    run(main())
    assert calls == [1]


def test_client_registers_and_cancels_tasks(monkeypatch):
    from test_client import patched

    client, _ = patched(monkeypatch)
    calls = []

    @loop(seconds=0.01)
    async def ticker(this):
        calls.append(1)

    client.add_task(ticker)
    assert client.tasks == [ticker]

    async def main():
        # start_task needs a running loop, so it happens inside
        client.start_task(ticker)
        await asyncio.sleep(0.05)
        client.cancel_tasks()
        await asyncio.sleep(0.02)
        return calls, ticker.is_running, list(client.tasks)

    seen, running, remaining = run(main())
    assert len(seen) >= 1
    assert running is False
    assert remaining == []


def test_client_close_cancels_tasks(monkeypatch):
    from test_client import patched

    client, gateway = patched(monkeypatch)

    @loop(seconds=0.05)
    async def ticker(this):
        pass

    client.add_task(ticker)

    class FakeHttp:
        async def close(self):
            pass

    client.http = FakeHttp()  # type: ignore[assignment]

    async def main():
        client.start_task(ticker)
        await asyncio.sleep(0.02)
        await client.close()
        return ticker.is_running, list(client.tasks)

    running, remaining = run(main())
    assert running is False
    assert remaining == []
