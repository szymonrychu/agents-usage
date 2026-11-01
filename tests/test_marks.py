"""Session marks from the orchestrator's session views (no database)."""

import uuid
from datetime import UTC, datetime

import pytest

from agents_usage import marks
from agents_usage.marks import Mark

A, B, C, D = (uuid.UUID(int=n) for n in (1, 2, 3, 4))
PRESET = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
AGENT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def mark(session_id: uuid.UUID) -> Mark:
    return Mark(session_id, "slug", None, None, "ui", datetime(2026, 11, 1, tzinfo=UTC))


@pytest.mark.parametrize(
    ("service", "session", "origin"),
    [
        (None, None, "ui"),
        ("agents-webhook", None, "webhook"),
        ("agents-schedule", None, "schedule"),
        ("agents-matrix-connector", None, "chat"),
        ("other-svc", None, "other"),
        (None, AGENT, "agent"),
        ("agents-webhook", AGENT, "agent"),
        ("", None, "ui"),
    ],
)
def test_origin_mapping(service, session, origin):
    assert marks.origin_of(service, session) == origin


def test_mark_from_session():
    view = {
        "id": str(A),
        "slug": "alpha",
        "preset_id": str(PRESET),
        "preset_name": "pr-review",
        "created_at": "2026-11-01T11:00:00+02:00",
        "created_by_service": "agents-webhook",
        "created_by_session": None,
        "hive": False,
        "summary": {"agents": 1},
    }
    assert marks.mark_from_session(view) == Mark(
        A, "alpha", PRESET, "pr-review", "webhook", datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    )


def test_mark_from_session_utc_z_and_naive():
    base = {"id": str(A), "slug": "alpha"}
    zulu = marks.mark_from_session({**base, "created_at": "2026-11-01T09:00:00Z"})
    naive = marks.mark_from_session({**base, "created_at": "2026-11-01T09:00:00"})
    expected = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert zulu.started_at == naive.started_at == expected
    assert zulu.started_at.utcoffset().total_seconds() == 0
    assert (zulu.preset_id, zulu.preset_name, zulu.origin) == (None, None, "ui")


@pytest.mark.parametrize(
    "view",
    [
        {"slug": "s", "created_at": "2026-11-01T09:00:00Z"},
        {"id": "not-a-uuid", "slug": "s", "created_at": "2026-11-01T09:00:00Z"},
        {"id": str(A), "created_at": "2026-11-01T09:00:00Z"},
        {"id": str(A), "slug": "", "created_at": "2026-11-01T09:00:00Z"},
        {"id": str(A), "slug": "s", "created_at": "nope"},
        {"id": str(A), "slug": "s"},
        {"id": 5, "slug": "s", "created_at": "2026-11-01T09:00:00Z"},
        None,
        "text",
    ],
)
def test_mark_from_session_bad(view):
    assert marks.mark_from_session(view) is None


def test_mark_from_session_bad_optional_fields_are_dropped():
    view = {
        "id": str(A),
        "slug": "s",
        "created_at": "2026-11-01T09:00:00Z",
        "preset_id": "nope",
        "preset_name": 5,
    }
    got = marks.mark_from_session(view)
    assert got is not None
    assert (got.preset_id, got.preset_name) == (None, None)


def test_plan_snapshot_inserts_unknown_and_closes_missing():
    plan = marks.plan_snapshot({A, B}, {A, B, C}, [mark(A), mark(D)])
    assert plan.to_insert == [mark(D)]
    assert plan.to_close == [B]


def test_plan_snapshot_never_reopens():
    plan = marks.plan_snapshot(set(), {C}, [mark(C)])
    assert plan.to_insert == []
    assert plan.to_close == []


def test_plan_snapshot_empty_snapshot_closes_all_open():
    plan = marks.plan_snapshot({B, A}, {A, B, C}, [])
    assert plan.to_insert == []
    assert plan.to_close == [A, B]


def test_plan_snapshot_duplicate_session_inserted_once():
    plan = marks.plan_snapshot(set(), set(), [mark(A), mark(A)])
    assert plan.to_insert == [mark(A)]
