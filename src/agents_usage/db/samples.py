"""Statusline samples: the ingest listener is the only writer of `samples` and `sample_windows`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agents_usage.parser import Window


@dataclass(frozen=True)
class SampleRecord:
    observed_at: datetime
    agent: str | None
    cli_version: str | None
    raw: dict
    windows: dict[str, Window]


async def insert_sample(conn: AsyncConnection, rec: SampleRecord) -> int:
    """Insert the sample and its windows in one transaction; returns the sample id."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO samples (observed_at, agent, cli_version, raw)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (rec.observed_at, rec.agent, rec.cli_version, Jsonb(rec.raw)),
        )
        row = await cur.fetchone()
        assert row is not None
        sample_id: int = row[0]
        async with conn.cursor() as windows:
            await windows.executemany(
                "INSERT INTO sample_windows (sample_id, metric, percent, resets_at)"
                " VALUES (%s, %s, %s, %s)",
                [(sample_id, w.metric, w.percent, w.resets_at) for w in rec.windows.values()],
            )
    return sample_id


async def prune(conn: AsyncConnection, older_than: datetime) -> int:
    """Delete the samples observed before `older_than` (their windows cascade); the count."""
    cur = await conn.execute("DELETE FROM samples WHERE observed_at < %s", (older_than,))
    return cur.rowcount
