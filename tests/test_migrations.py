"""Schema checks; they need Postgres (testcontainers), so CI is their evidence."""

import uuid

import psycopg
import pytest

from agents_usage.db.migrate import migrate

NOW = "2026-11-01T12:00:00Z"
RESET = "2026-11-01T17:00:00Z"


def add_sample(conn, windows=(("five_hour", 12.5), ("seven_day", 40.0)), agent="agent-1") -> int:
    sample_id = conn.execute(
        "INSERT INTO samples (observed_at, agent, raw) VALUES (%s, %s, '{}') RETURNING id",
        (NOW, agent),
    ).fetchone()[0]
    for metric, percent in windows:
        conn.execute(
            "INSERT INTO sample_windows (sample_id, metric, percent, resets_at) "
            "VALUES (%s, %s, %s, %s)",
            (sample_id, metric, percent, RESET),
        )
    return sample_id


def add_probe(conn, ok=True, windows=(("five_hour", 20.0),)) -> int:
    error = None if ok else "timeout"
    probe_id = conn.execute(
        "INSERT INTO probes (started_at, observed_at, ok, error, duration_ms) "
        "VALUES (%s, %s, %s, %s, 1500) RETURNING id",
        (NOW, NOW, ok, error),
    ).fetchone()[0]
    for metric, percent in windows:
        conn.execute(
            "INSERT INTO probe_windows (probe_id, metric, percent, resets_at) "
            "VALUES (%s, %s, %s, %s)",
            (probe_id, metric, percent, RESET),
        )
    return probe_id


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_migrate_creates_tables(empty_db_url, settings_for):
    settings = settings_for(empty_db_url)
    assert migrate(settings) == ["0001_init.sql"]
    assert migrate(settings) == []
    with psycopg.connect(empty_db_url) as conn:
        names = lambda kind: {  # noqa: E731
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = %s",
                (kind,),
            )
        }
        tables = names("BASE TABLE")
        views = names("VIEW")
    assert {"samples", "sample_windows", "probes", "probe_windows", "session_marks"} <= tables
    assert "readings" in views


def test_windows_cascade(conn):
    sample_id = add_sample(conn)
    probe_id = add_probe(conn)
    assert count(conn, "sample_windows") == 2 and count(conn, "probe_windows") == 1
    conn.execute("DELETE FROM samples WHERE id = %s", (sample_id,))
    assert count(conn, "sample_windows") == 0
    conn.execute("DELETE FROM probes WHERE id = %s", (probe_id,))
    assert count(conn, "probe_windows") == 0


@pytest.mark.parametrize("percent", [-0.1, 1000.1])
def test_percent_out_of_bounds_refused(conn, percent):
    with pytest.raises(psycopg.errors.CheckViolation):
        add_sample(conn, windows=(("five_hour", percent),))


def test_percent_over_one_hundred_accepted(conn):
    add_sample(conn, windows=(("five_hour", 103.0),))
    assert count(conn, "sample_windows") == 1


def test_readings_view(conn):
    add_sample(conn)
    add_probe(conn)
    add_probe(conn, ok=False, windows=())
    rows = conn.execute("SELECT source, agent, metric FROM readings").fetchall()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {"statusline", "probe"}
    assert [r[1] for r in rows if r[0] == "probe"] == [None]
    assert {r[1] for r in rows if r[0] == "statusline"} == {"agent-1"}


def test_failed_probe_needs_error_and_ok_probe_must_not_have_one(conn):
    insert = (
        "INSERT INTO probes (started_at, observed_at, ok, error, duration_ms) "
        "VALUES (%s, %s, %s, %s, 1)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(insert, (NOW, NOW, False, None))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(insert, (NOW, NOW, True, "timeout"))


def mark(conn, session_id=None, origin="ui", started="2026-11-01T10:00:00Z", ended=None):
    conn.execute(
        "INSERT INTO session_marks (session_id, slug, origin, started_at, ended_at) "
        "VALUES (%s, 'slug', %s, %s, %s)",
        (session_id or uuid.uuid4(), origin, started, ended),
    )


def test_session_marks_constraints(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        mark(conn, origin="bogus")
    with pytest.raises(psycopg.errors.CheckViolation):
        mark(conn, started="2026-11-01T10:00:00Z", ended="2026-11-01T09:00:00Z")
    same = uuid.uuid4()
    mark(conn, session_id=same)
    with pytest.raises(psycopg.errors.UniqueViolation):
        mark(conn, session_id=same)
    mark(conn, ended=None)
    assert count(conn, "session_marks") == 2
