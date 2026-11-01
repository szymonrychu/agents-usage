"""The probe trigger and where it is wired. No database: a pool stub records the NOTIFYs and the
reads are replaced."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from conftest import asgi_client

from agents_usage.api import usage
from agents_usage.api.app import PREFIX, create_app, create_service_app
from agents_usage.api.usage import ProbeTrigger
from agents_usage.db.probes import PROBE_CHANNEL

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
AFTER = 1800


class RecordingConn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, query, params=None):
        self.pool.statements.append((query, params))


class RecordingPool:
    def __init__(self, error=None):
        self.statements = []
        self.error = error

    @asynccontextmanager
    async def connection(self):
        if self.error:
            raise self.error
        yield RecordingConn(self)

    @property
    def notifies(self):
        return [p for q, p in self.statements if "pg_notify" in q]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def trigger(pool, clock=None, min_gap=60):
    return ProbeTrigger(pool, AFTER, clock or Clock(), min_gap)


async def test_notifies_when_stale():
    pool = RecordingPool()
    assert await trigger(pool).maybe(NOW - timedelta(seconds=1801), NOW) is True
    assert pool.notifies == [(PROBE_CHANNEL, "")]


async def test_notifies_when_no_data():
    pool = RecordingPool()
    assert await trigger(pool).maybe(None, NOW) is True
    assert len(pool.notifies) == 1


async def test_quiet_when_fresh():
    pool = RecordingPool()
    assert await trigger(pool).maybe(NOW - timedelta(seconds=60), NOW) is False
    assert await trigger(pool).maybe(NOW - timedelta(seconds=1800), NOW) is False
    assert pool.notifies == []


async def test_one_notify_per_minute():
    pool, clock = RecordingPool(), Clock()
    t = trigger(pool, clock)
    stale = NOW - timedelta(hours=1)
    assert await t.maybe(stale, NOW) is True
    clock.now += 30
    assert await t.maybe(stale, NOW) is False
    clock.now += 29
    assert await t.maybe(stale, NOW) is False
    assert len(pool.notifies) == 1
    clock.now += 2
    assert await t.maybe(stale, NOW) is True
    assert len(pool.notifies) == 2


async def test_notify_failure_swallowed(caplog):
    pool = RecordingPool(error=RuntimeError("database is down"))
    with caplog.at_level(logging.WARNING):
        assert await trigger(pool).maybe(None, NOW) is False
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.fixture
def stale_reads(monkeypatch):
    """The four reads of `current` answer with a reading older than the probe threshold."""
    old = datetime.now(UTC) - timedelta(hours=2)

    async def groups(conn, now):
        return []

    async def newest(conn):
        return old

    async def none(conn):
        return None

    monkeypatch.setattr(usage.readings, "window_groups", groups)
    monkeypatch.setattr(usage.readings, "newest_at", newest)
    monkeypatch.setattr(usage.samples, "latest", none)
    monkeypatch.setattr(usage.probes, "latest", none)


async def test_ui_current_never_triggers(settings, stale_reads):
    pool = RecordingPool()
    app = create_app(settings, pool)
    assert app.state.probe_trigger is None
    async with asgi_client(app) as http:
        response = await http.get(f"{PREFIX}/current")
    assert response.status_code == 200
    assert pool.notifies == []


async def test_service_current_triggers(settings, stale_reads):
    pool = RecordingPool()
    app = create_service_app(settings, pool, ["t"])
    async with asgi_client(app, peer="10.9.9.9") as http:
        response = await http.get(f"{PREFIX}/current", headers={"Authorization": "Bearer t"})
        assert response.status_code == 200
        for _ in range(50):
            if pool.notifies:
                break
            await asyncio.sleep(0.01)
    assert pool.notifies == [(PROBE_CHANNEL, "")]


async def test_service_current_does_not_wait(settings, stale_reads):
    pool = RecordingPool()
    app = create_service_app(settings, pool, ["t"])
    blocked = asyncio.Event()

    class Blocking:
        async def maybe(self, newest_at, now):
            await blocked.wait()
            return True

    app.state.probe_trigger = Blocking()
    async with asgi_client(app, peer="10.9.9.9") as http:
        response = await asyncio.wait_for(
            http.get(f"{PREFIX}/current", headers={"Authorization": "Bearer t"}), timeout=1
        )
    assert response.status_code == 200
    for task in list(app.state.background):
        task.cancel()
