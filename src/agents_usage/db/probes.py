"""Probe attempts: the prober is the only writer of `probes` and `probe_windows`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agents_usage.parser import Window

PROBE_CHANNEL = "agents_usage_probe"


@dataclass(frozen=True)
class ProbeRecord:
    started_at: datetime
    observed_at: datetime
    ok: bool
    error: str | None
    detail: str | None
    cli_version: str | None
    cost_usd: float | None
    duration_ms: int
    raw: dict | None
    windows: dict[str, Window]


@dataclass(frozen=True)
class ProbeRow:
    started_at: datetime
    ok: bool
    error: str | None


async def insert_probe(conn: AsyncConnection, rec: ProbeRecord) -> int:
    """Insert the attempt and its windows in one transaction; returns the probe id."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO probes (started_at, observed_at, ok, error, detail, cli_version,"
            " cost_usd, duration_ms, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (
                rec.started_at,
                rec.observed_at,
                rec.ok,
                rec.error,
                rec.detail,
                rec.cli_version,
                rec.cost_usd,
                rec.duration_ms,
                None if rec.raw is None else Jsonb(rec.raw),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        probe_id: int = row[0]
        async with conn.cursor() as windows:
            await windows.executemany(
                "INSERT INTO probe_windows (probe_id, metric, percent, resets_at)"
                " VALUES (%s, %s, %s, %s)",
                [(probe_id, w.metric, w.percent, w.resets_at) for w in rec.windows.values()],
            )
    return probe_id


async def prune(conn: AsyncConnection, older_than: datetime) -> int:
    """Delete the probes observed before `older_than` (their windows cascade); the count."""
    cur = await conn.execute("DELETE FROM probes WHERE observed_at < %s", (older_than,))
    return cur.rowcount


async def latest(conn: AsyncConnection) -> ProbeRow | None:
    """The newest probe attempt, failed or not."""
    cur = await conn.execute(
        "SELECT started_at, ok, error FROM probes ORDER BY started_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    return None if row is None else ProbeRow(*row)


async def probe_due(
    conn: AsyncConnection, now: datetime, after_seconds: int, min_interval_seconds: int
) -> bool:
    """True when no reading is newer than `now - after_seconds` and no probe attempt (failed
    ones count) started after `now - min_interval_seconds`."""
    cur = await conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM readings WHERE observed_at > %s)"
        " AND NOT EXISTS (SELECT 1 FROM probes WHERE started_at > %s)",
        (now - timedelta(seconds=after_seconds), now - timedelta(seconds=min_interval_seconds)),
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])
