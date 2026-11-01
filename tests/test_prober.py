"""The prober loop: only a request starts a probe, one at a time (no database)."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from agents_usage import prober


async def requests_at(delays: list[float], then_wait: float = 0.3):
    """Yield one request after each delay (seconds since the previous one), then stay open."""
    for delay in delays:
        await asyncio.sleep(delay)
        yield ""
    await asyncio.sleep(then_wait)


class Recorder:
    """A fake `handle` that records calls and whether two of them ever overlapped."""

    def __init__(self, seconds: float = 0.05, fail_first: bool = False, probed: bool = False):
        self.seconds = seconds
        self.fail_first = fail_first
        self.probed = probed
        self.calls = 0
        self.running = 0
        self.overlap = False

    async def __call__(self, pool, settings) -> bool:
        self.calls += 1
        self.running += 1
        self.overlap = self.overlap or self.running > 1
        try:
            await asyncio.sleep(self.seconds)
            if self.fail_first and self.calls == 1:
                raise RuntimeError("database is down")
            return self.probed
        finally:
            self.running -= 1


async def run_until(settings, requests, handle, seconds: float, pool=None) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(
        prober.run_prober(pool, settings, stop, requests=requests, handle=handle)
    )
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(loop, 2)


async def test_no_request_never_probes(settings):
    handle = Recorder()
    await run_until(settings, requests_at([], then_wait=5), handle, 0.2)
    assert handle.calls == 0


async def test_requests_handled_one_at_a_time(settings):
    # the second request arrives while the first is handled, the third after both
    handle = Recorder(seconds=0.05)
    await run_until(settings, requests_at([0, 0.02, 0.2]), handle, 0.5)
    assert handle.calls == 3
    assert handle.overlap is False


async def test_burst_coalesced(settings):
    # five requests while one is handled give one more call, not five
    handle = Recorder(seconds=0.2)
    await run_until(settings, requests_at([0, 0.02, 0.02, 0.02, 0.02, 0.02]), handle, 0.7)
    assert handle.calls == 2
    assert handle.overlap is False


async def test_handle_error_keeps_loop(settings, caplog):
    handle = Recorder(seconds=0.01, fail_first=True)
    with caplog.at_level(logging.ERROR, logger="agents_usage.prober"):
        await run_until(settings, requests_at([0, 0.1]), handle, 0.3)
    assert handle.calls == 2
    assert "database is down" in caplog.text


async def test_stop_while_idle_returns(settings):
    stop = asyncio.Event()
    loop = asyncio.create_task(
        prober.run_prober(None, settings, stop, requests=requests_at([], 60), handle=Recorder())
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(loop, 1)


async def test_requests_end_returns_after_pending(settings):
    async def two():
        yield ""
        yield ""

    handle = Recorder(seconds=0)
    await asyncio.wait_for(
        prober.run_prober(None, settings, asyncio.Event(), requests=two(), handle=handle), 1
    )
    assert handle.calls == 1  # both were pending at once: coalesced


async def test_requests_failure_propagates(settings):
    async def broken():
        raise RuntimeError("listen failed")
        yield ""  # pragma: no cover

    try:
        await asyncio.wait_for(
            prober.run_prober(None, settings, asyncio.Event(), requests=broken()), 1
        )
    except RuntimeError as exc:
        assert "listen failed" in str(exc)
    else:
        raise AssertionError("the requests failure was swallowed")


class FakeConn:
    def __init__(self, log: list) -> None:
        self.log = log

    async def execute(self, query, params=None):
        self.log.append((query, params))

        class Cursor:
            rowcount = 0

        return Cursor()


class FakePool:
    def __init__(self) -> None:
        self.statements: list = []

    @contextlib.asynccontextmanager
    async def connection(self):
        yield FakeConn(self.statements)


async def test_prune_after_a_probe_at_most_hourly(settings):
    pool = FakePool()
    before = datetime.now(UTC)
    handle = Recorder(seconds=0, probed=True)
    await run_until(settings, requests_at([0, 0.05, 0.05]), handle, 0.3, pool=pool)
    assert handle.calls == 3
    deletes = [p for q, p in pool.statements if q.startswith("DELETE FROM probes")]
    assert len(deletes) == 1
    cutoff = deletes[0][0]
    expected = before - timedelta(days=settings.retention_days)
    assert abs((cutoff - expected).total_seconds()) < 5


async def test_no_prune_without_a_probe(settings):
    pool = FakePool()
    await run_until(settings, requests_at([0]), Recorder(seconds=0), 0.1, pool=pool)
    assert pool.statements == []
