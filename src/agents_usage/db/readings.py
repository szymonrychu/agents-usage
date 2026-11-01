"""Reads over the `readings` view (statusLine samples and successful probes).

The API never writes here: the api writes `samples`, the prober `probes`, and both are read
through the view, so no table has two writers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import AsyncConnection

WINDOW_LOOKBACK = timedelta(days=8)


@dataclass(frozen=True)
class WindowGroup:
    """The readings of one metric and one window (`resets_at` to the minute): the highest
    percent, and the newest reading's time, source and exact reset."""

    metric: str
    resets_at: datetime
    percent: float
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class PointRow:
    observed_at: datetime
    metric: str
    percent: float
    resets_at: datetime
    source: str


# Readings of the lookback that have not reset yet, one row per metric and window. The
# observed_at bound keeps both underlying tables on their observed_at indexes.
WINDOW_GROUPS_SQL = """
SELECT metric,
       (array_agg(resets_at ORDER BY observed_at DESC))[1] AS resets_at,
       max(percent) AS percent,
       max(observed_at) AS observed_at,
       (array_agg(source ORDER BY observed_at DESC))[1] AS source
FROM readings
WHERE observed_at > %(since)s AND resets_at > %(now)s
GROUP BY metric, date_trunc('minute', resets_at)
"""

# The newest reading's time without scanning the view: both tables have an observed_at index.
# It must agree with the view, so a probe counts only when it is ok and has a window (the parser
# can leave an ok probe without windows); a statusLine sample always has one, the ingest refuses
# others.
NEWEST_SQL = """
SELECT greatest(
    (SELECT max(observed_at) FROM samples),
    (SELECT p.observed_at FROM probes p
     WHERE p.ok AND EXISTS (SELECT 1 FROM probe_windows w WHERE w.probe_id = p.id)
     ORDER BY p.observed_at DESC LIMIT 1)
)
"""

# One query: the peak reading (highest percent, ties to the latest) of every metric and
# bucket, as the real row, oldest first. A bucket is `width` seconds counted from `frm`.
HISTORY_SQL = """
SELECT observed_at, metric, percent, resets_at, source FROM (
    SELECT DISTINCT ON (metric, bucket) observed_at, metric, percent, resets_at, source
    FROM (
        SELECT observed_at, metric, percent, resets_at, source,
               floor((extract(epoch FROM observed_at) - extract(epoch FROM %(frm)s::timestamptz))
                     / %(width)s) AS bucket
        FROM readings
        WHERE observed_at >= %(frm)s AND observed_at < %(to)s
          AND (%(metric)s::text IS NULL OR metric = %(metric)s::text)
    ) r
    ORDER BY metric, bucket, percent DESC, observed_at DESC
) d
ORDER BY observed_at, metric
"""


async def window_groups(conn: AsyncConnection, now: datetime) -> list[WindowGroup]:
    """The window groups that matter for `current`: observed within the lookback and not reset."""
    cur = await conn.execute(WINDOW_GROUPS_SQL, {"since": now - WINDOW_LOOKBACK, "now": now})
    return [
        WindowGroup(metric, resets_at, percent, observed_at, source)
        for metric, resets_at, percent, observed_at, source in await cur.fetchall()
    ]


async def newest_at(conn: AsyncConnection) -> datetime | None:
    """When the newest reading (any source) was observed, or None before the first."""
    cur = await conn.execute(NEWEST_SQL)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def history(
    conn: AsyncConnection, metric: str | None, frm: datetime, to: datetime, width: int
) -> list[PointRow]:
    """The downsampled readings in [frm, to), oldest first."""
    cur = await conn.execute(HISTORY_SQL, {"metric": metric, "frm": frm, "to": to, "width": width})
    return [PointRow(*row) for row in await cur.fetchall()]
