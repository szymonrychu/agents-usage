"""Sample storage against Postgres (CI only: needs Docker)."""

from datetime import UTC, datetime, timedelta

from agents_usage.db import samples
from agents_usage.parser import Window

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
RAW = {
    "five_hour": {"used_percentage": 23.5, "resets_at": 1794020400},
    "seven_day": {"used_percentage": 22, "resets_at": 1794049200},
}


def record(observed_at: datetime = NOW) -> samples.SampleRecord:
    windows = {
        "five_hour": Window("five_hour", 23.5, datetime.fromtimestamp(1794020400, UTC)),
        "seven_day": Window("seven_day", 22.0, datetime.fromtimestamp(1794049200, UTC)),
    }
    return samples.SampleRecord(observed_at, "agent-x-0", "2.1.304", RAW, windows)


async def test_insert_sample_with_windows(pool):
    async with pool.connection() as conn:
        sample_id = await samples.insert_sample(conn, record())
    async with pool.connection() as conn:
        raw = (
            await (
                await conn.execute("SELECT raw FROM samples WHERE id = %s", (sample_id,))
            ).fetchone()
        )[0]
        windows = await (
            await conn.execute("SELECT metric, percent FROM sample_windows ORDER BY metric")
        ).fetchall()
        readings = await (
            await conn.execute("SELECT source, agent, metric FROM readings ORDER BY metric")
        ).fetchall()
    assert raw == RAW
    assert windows == [("five_hour", 23.5), ("seven_day", 22.0)]
    assert readings == [
        ("statusline", "agent-x-0", "five_hour"),
        ("statusline", "agent-x-0", "seven_day"),
    ]


async def test_prune(pool):
    async with pool.connection() as conn:
        await samples.insert_sample(conn, record(NOW - timedelta(days=100)))
        await samples.insert_sample(conn, record(NOW))
    async with pool.connection() as conn:
        removed = await samples.prune(conn, NOW - timedelta(days=90))
    async with pool.connection() as conn:
        left = (await (await conn.execute("SELECT count(*) FROM samples")).fetchone())[0]
        windows = (await (await conn.execute("SELECT count(*) FROM sample_windows")).fetchone())[0]
    assert (removed, left, windows) == (1, 1, 2)
