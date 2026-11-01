"""Session marks: the watcher is the only writer of `session_marks`.

Marks are insert-only (a known session is never inserted again or reopened); the only update is
setting `ended_at` once. Only closed marks are ever purged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agents_usage.marks import Mark, SnapshotPlan, plan_snapshot


@dataclass(frozen=True)
class MarkRow:
    """A stored mark; `ended_at` is None while the session is open."""

    session_id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None
    preset_name: str | None
    origin: str
    started_at: datetime
    ended_at: datetime | None


OVERLAPPING_SQL = (
    "SELECT session_id, slug, preset_id, preset_name, origin, started_at, ended_at"
    " FROM session_marks"
    " WHERE started_at < %s AND (ended_at IS NULL OR ended_at > %s)"
    " ORDER BY started_at, session_id LIMIT %s"
)


async def insert_mark(conn: AsyncConnection, mark: Mark) -> bool:
    """Insert the mark; False when the session already has one (open or closed)."""
    cur = await conn.execute(
        "INSERT INTO session_marks (session_id, slug, preset_id, preset_name, origin, started_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
        (
            mark.session_id,
            mark.slug,
            mark.preset_id,
            mark.preset_name,
            mark.origin,
            mark.started_at,
        ),
    )
    return cur.rowcount == 1


async def close_mark(conn: AsyncConnection, session_id: uuid.UUID, now: datetime) -> bool:
    """End an open mark at `now` (never before its start); False when none is open."""
    cur = await conn.execute(
        "UPDATE session_marks SET ended_at = greatest(%s, started_at)"
        " WHERE session_id = %s AND ended_at IS NULL",
        (now, session_id),
    )
    return cur.rowcount == 1


async def apply_snapshot(conn: AsyncConnection, plan: SnapshotPlan, now: datetime) -> None:
    """Apply a snapshot plan in one transaction."""
    async with conn.transaction():
        for mark in plan.to_insert:
            await insert_mark(conn, mark)
        for session_id in plan.to_close:
            await close_mark(conn, session_id, now)


async def purge(conn: AsyncConnection, older_than: datetime) -> int:
    """Delete the marks closed before `older_than`; an open mark is never deleted."""
    cur = await conn.execute("DELETE FROM session_marks WHERE ended_at < %s", (older_than,))
    return cur.rowcount


async def overlapping(
    conn: AsyncConnection, frm: datetime, to: datetime, limit: int
) -> list[MarkRow]:
    """The marks that overlap `[frm, to)`, oldest start first: started before `to` and still open
    or ended after `frm`. Fetches `limit + 1` rows so the caller can tell a cut-off list."""
    cur = await conn.execute(OVERLAPPING_SQL, (to, frm, limit + 1))
    return [MarkRow(*row) for row in await cur.fetchall()]


async def open_and_known_ids(conn: AsyncConnection) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """The ids of the open marks and of all marks."""
    cur = await conn.execute("SELECT session_id, ended_at IS NULL FROM session_marks")
    rows = await cur.fetchall()
    return {r[0] for r in rows if r[1]}, {r[0] for r in rows}


class MarksRepo:
    """What the watcher needs from the table, over a pool (tests substitute a fake)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def reconcile(self, snapshot: list[Mark], live: set[uuid.UUID], now: datetime) -> None:
        """Bring the table in line with a full list of live sessions (`snapshot` the parsed
        marks, `live` every listed id), in one transaction."""
        async with self.pool.connection() as conn, conn.transaction():
            open_ids, known_ids = await open_and_known_ids(conn)
            await apply_snapshot(conn, plan_snapshot(open_ids, known_ids, snapshot, live), now)

    async def insert(self, mark: Mark) -> bool:
        async with self.pool.connection() as conn:
            return await insert_mark(conn, mark)

    async def close(self, session_id: uuid.UUID, now: datetime) -> bool:
        async with self.pool.connection() as conn:
            return await close_mark(conn, session_id, now)

    async def purge(self, older_than: datetime) -> int:
        async with self.pool.connection() as conn:
            return await purge(conn, older_than)
