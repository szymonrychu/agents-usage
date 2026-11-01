"""The session_marks repository over a migrated database (needs Postgres)."""

import uuid
from datetime import UTC, datetime, timedelta

from agents_usage import marks
from agents_usage.db import marks as repo
from agents_usage.marks import Mark

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
A, B, C, D = (uuid.UUID(int=n) for n in (1, 2, 3, 4))


def mark(session_id, started_at=NOW - timedelta(hours=3), origin="ui") -> Mark:
    return Mark(session_id, f"slug-{session_id.int}", None, None, origin, started_at)


async def rows(pool):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, slug, origin, started_at, ended_at FROM session_marks"
            " ORDER BY session_id"
        )
        return await cur.fetchall()


async def test_apply_snapshot_inserts_and_closes(pool):
    async with pool.connection() as conn:
        await repo.insert_mark(conn, mark(A))
        await repo.insert_mark(conn, mark(B))
    created = NOW - timedelta(hours=1)
    plan = marks.SnapshotPlan(to_insert=[mark(D, created, "webhook")], to_close=[B])
    async with pool.connection() as conn:
        await repo.apply_snapshot(conn, plan, NOW)
    by_id = {r[0]: r for r in await rows(pool)}
    assert by_id[B][4] == NOW
    assert by_id[A][4] is None
    assert by_id[D][2:] == ("webhook", created, None)


async def test_snapshot_idempotent(pool):
    plan = marks.SnapshotPlan(to_insert=[mark(A), mark(D)], to_close=[])
    async with pool.connection() as conn:
        await repo.apply_snapshot(conn, plan, NOW)
    first = await rows(pool)
    async with pool.connection() as conn:
        await repo.apply_snapshot(conn, plan, NOW + timedelta(hours=1))
    assert await rows(pool) == first
    assert len(first) == 2


async def test_open_and_known_ids(pool):
    async with pool.connection() as conn:
        await repo.insert_mark(conn, mark(A))
        await repo.insert_mark(conn, mark(B))
        await repo.close_mark(conn, B, NOW)
        open_ids, known = await repo.open_and_known_ids(conn)
    assert (open_ids, known) == ({A}, {A, B})


async def test_closed_mark_not_reopened(pool):
    async with pool.connection() as conn:
        assert await repo.insert_mark(conn, mark(A)) is True
        assert await repo.close_mark(conn, A, NOW) is True
        assert await repo.insert_mark(conn, mark(A, NOW - timedelta(days=1))) is False
    (row,) = await rows(pool)
    assert row[4] == NOW


async def test_close_mark_only_open(pool):
    async with pool.connection() as conn:
        await repo.insert_mark(conn, mark(A))
        assert await repo.close_mark(conn, A, NOW) is True
        assert await repo.close_mark(conn, A, NOW + timedelta(hours=1)) is False
        assert await repo.close_mark(conn, B, NOW) is False
    (row,) = await rows(pool)
    assert row[4] == NOW


async def test_close_never_precedes_the_start(pool):
    """A clock behind the orchestrator's created_at must not break the ended_at check."""
    async with pool.connection() as conn:
        await repo.insert_mark(conn, mark(A, NOW + timedelta(minutes=5)))
        assert await repo.close_mark(conn, A, NOW) is True
    (row,) = await rows(pool)
    assert row[4] == row[3] == NOW + timedelta(minutes=5)


async def test_purge_closed_only(pool):
    async with pool.connection() as conn:
        for session_id, started, ended in (
            (A, NOW - timedelta(days=95), NOW - timedelta(days=91)),
            (B, NOW - timedelta(days=95), NOW - timedelta(days=89)),
            (C, NOW - timedelta(days=200), None),
        ):
            await repo.insert_mark(conn, mark(session_id, started))
            if ended:
                await repo.close_mark(conn, session_id, ended)
        removed = await repo.purge(conn, NOW - timedelta(days=90))
    assert removed == 1
    assert [r[0] for r in await rows(pool)] == [B, C]


async def test_marks_repo_wraps_the_pool(pool):
    store = repo.MarksRepo(pool)
    await store.reconcile([mark(A), mark(B)], NOW)
    assert await store.insert(mark(C)) is True
    assert await store.insert(mark(C)) is False
    assert await store.close(A, NOW) is True
    await store.reconcile([mark(B), mark(D)], NOW + timedelta(minutes=1))
    by_id = {r[0]: r[4] for r in await rows(pool)}
    assert by_id == {A: NOW, B: None, C: NOW + timedelta(minutes=1), D: None}
    assert await store.purge(NOW + timedelta(days=1)) == 2
