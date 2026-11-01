"""Probe storage and the due rule against Postgres (CI only: needs Docker)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agents_usage.db import probes, samples
from agents_usage.parser import Window

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
RESET = datetime.fromtimestamp(1794049200, UTC)
EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "rateLimitType": "seven_day",
        "unifiedWindows": {"seven_day": {"utilization": 0.22, "resetsAt": 1794049200}},
    },
}


def ok_probe(at: datetime = NOW) -> probes.ProbeRecord:
    return probes.ProbeRecord(
        started_at=at - timedelta(seconds=2),
        observed_at=at,
        ok=True,
        error=None,
        detail=None,
        cli_version="2.1.304",
        cost_usd=0.0043312,
        duration_ms=1934,
        raw=EVENT,
        windows={"seven_day": Window("seven_day", 22.0, RESET)},
    )


def failed_probe(started_at: datetime) -> probes.ProbeRecord:
    return probes.ProbeRecord(
        started_at, started_at, False, "timeout", None, None, None, 120000, None, {}
    )


def sample(observed_at: datetime) -> samples.SampleRecord:
    raw = {"seven_day": {"used_percentage": 22, "resets_at": 1794049200}}
    windows = {"seven_day": Window("seven_day", 22.0, RESET)}
    return samples.SampleRecord(observed_at, "agent-x-0", "2.1.304", raw, windows)


async def test_insert_probe_with_windows(pool):
    async with pool.connection() as conn:
        probe_id = await probes.insert_probe(conn, ok_probe())
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT ok, error, cli_version, cost_usd, duration_ms, raw FROM probes"
                " WHERE id = %s",
                (probe_id,),
            )
        ).fetchone()
        windows = await (
            await conn.execute("SELECT probe_id, metric, percent, resets_at FROM probe_windows")
        ).fetchall()
        readings = await (
            await conn.execute("SELECT source, agent, metric, percent FROM readings")
        ).fetchall()
    ok, error, cli_version, cost_usd, duration_ms, raw = row
    assert (ok, error, cli_version, duration_ms) == (True, None, "2.1.304", 1934)
    assert float(cost_usd) == 0.004331
    assert raw == EVENT
    assert windows == [(probe_id, "seven_day", 22.0, RESET)]
    assert readings == [("probe", None, "seven_day", 22.0)]


async def test_insert_failed_probe(pool):
    async with pool.connection() as conn:
        probe_id = await probes.insert_probe(conn, failed_probe(NOW))
        latest = await probes.latest(conn)
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT ok, error, raw FROM probes WHERE id = %s", (probe_id,))
        ).fetchone()
        windows = (await (await conn.execute("SELECT count(*) FROM probe_windows")).fetchone())[0]
        readings = (await (await conn.execute("SELECT count(*) FROM readings")).fetchone())[0]
    assert row == (False, "timeout", None)
    assert (windows, readings) == (0, 0)
    assert latest == probes.ProbeRow(NOW, False, "timeout")


async def test_latest(pool):
    async with pool.connection() as conn:
        assert await probes.latest(conn) is None
        await probes.insert_probe(conn, failed_probe(NOW - timedelta(hours=1)))
        await probes.insert_probe(conn, ok_probe(NOW))
        await probes.insert_probe(conn, failed_probe(NOW - timedelta(hours=2)))
        latest = await probes.latest(conn)
    assert latest == probes.ProbeRow(NOW - timedelta(seconds=2), True, None)


async def test_prune(pool):
    async with pool.connection() as conn:
        await probes.insert_probe(conn, ok_probe(NOW - timedelta(days=100)))
        await probes.insert_probe(conn, ok_probe(NOW))
    async with pool.connection() as conn:
        removed = await probes.prune(conn, NOW - timedelta(days=90))
    async with pool.connection() as conn:
        left = (await (await conn.execute("SELECT count(*) FROM probes")).fetchone())[0]
        windows = (await (await conn.execute("SELECT count(*) FROM probe_windows")).fetchone())[0]
    assert (removed, left, windows) == (1, 1, 1)


async def test_detail_of_2000_multibyte_characters_fits(pool):
    wide = replace(failed_probe(NOW), error="cli_exit", detail="\u017c" * 2000)
    async with pool.connection() as conn:
        probe_id = await probes.insert_probe(conn, wide)
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT char_length(detail) FROM probes WHERE id = %s", (probe_id,))
        ).fetchone()
    assert row == (2000,)


async def test_probe_due_rules(pool):
    async def due(conn) -> bool:
        return await probes.probe_due(conn, NOW, 1800, 1800)

    def minutes(n: int) -> datetime:
        return NOW - timedelta(minutes=n)

    async with pool.connection() as conn:
        assert await due(conn)  # empty database
        await samples.insert_sample(conn, sample(minutes(10)))
        assert not await due(conn)  # a fresh statusline sample
        await conn.execute("DELETE FROM samples")
        await samples.insert_sample(conn, sample(minutes(30)))
        assert await due(conn)  # exactly 30 min old counts as old
        await samples.insert_sample(conn, sample(minutes(31)))
        assert await due(conn)  # 31 min old and no probe
        await probes.insert_probe(conn, failed_probe(minutes(10)))
        assert not await due(conn)  # a failed probe 10 min ago counts for the interval
        await conn.execute("DELETE FROM probes")
        await probes.insert_probe(conn, failed_probe(minutes(31)))
        assert await due(conn)  # that probe 31 min ago
        await probes.insert_probe(conn, ok_probe(minutes(5)))
        assert not await due(conn)  # a fresh successful probe is a fresh reading
