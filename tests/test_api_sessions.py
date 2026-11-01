"""GET /usage/v1/sessions over a migrated database (needs Postgres)."""

import uuid
from datetime import UTC, datetime, timedelta

from conftest import asgi_client

from agents_usage.api.app import PREFIX, create_service_app
from agents_usage.db import marks as marks_db

SESSIONS = f"{PREFIX}/sessions"
T = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)
PRESET = uuid.UUID("11111111-1111-1111-1111-111111111111")


def span(frm: datetime, to: datetime, **extra: str) -> dict[str, str]:
    return {"from": frm.isoformat(), "to": to.isoformat(), **extra}


async def add_mark(pool, slug, started_at, ended_at=None, **fields):
    session_id = uuid.uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO session_marks"
            " (session_id, slug, preset_id, preset_name, origin, started_at, ended_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                slug,
                fields.get("preset_id"),
                fields.get("preset_name"),
                fields.get("origin", "ui"),
                started_at,
                ended_at,
            ),
        )
    return session_id


def slugs(body) -> list[str]:
    return [m["slug"] for m in body["marks"]]


async def test_overlap_predicate(client, pool):
    hour = timedelta(hours=1)
    await add_mark(pool, "ended-before", T - 3 * hour, T - hour)
    await add_mark(pool, "ended-at-from", T - 3 * hour, T)
    await add_mark(pool, "spans-from", T - hour, T + hour)
    await add_mark(pool, "inside", T + hour, T + 2 * hour)
    await add_mark(pool, "open-before", T - 5 * DAY)
    await add_mark(pool, "open-inside", T + 2 * hour)
    await add_mark(pool, "starts-at-to", T + DAY, T + DAY + hour)
    await add_mark(pool, "starts-after-to", T + DAY + hour)
    body = (await client.get(SESSIONS, params=span(T, T + DAY))).json()
    assert slugs(body) == ["open-before", "spans-from", "inside", "open-inside"]
    assert body["truncated"] is False


async def test_open_mark_ended_at_null(client, pool):
    await add_mark(pool, "open", T + timedelta(hours=1))
    (mark,) = (await client.get(SESSIONS, params=span(T, T + DAY))).json()["marks"]
    assert "ended_at" in mark
    assert mark["ended_at"] is None


async def test_order_and_shape(client, pool):
    ended = T + timedelta(hours=5)
    for slug, hours in (("c", 3), ("a", 1), ("b", 2)):
        await add_mark(pool, slug, T + timedelta(hours=hours), ended)
    first = await add_mark(
        pool, "d", T + timedelta(hours=4), preset_id=PRESET, preset_name="reviewer", origin="agent"
    )
    response = await client.get(SESSIONS, params=span(T, T + DAY))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"from", "to", "truncated", "marks"}
    assert slugs(body) == ["a", "b", "c", "d"]
    for mark in body["marks"]:
        assert set(mark) == {
            "session_id",
            "slug",
            "preset_id",
            "preset_name",
            "origin",
            "started_at",
            "ended_at",
        }
    assert datetime.fromisoformat(body["from"]) == T
    assert datetime.fromisoformat(body["to"]) == T + DAY
    last = body["marks"][3]
    assert last["session_id"] == str(first)
    assert last["preset_id"] == str(PRESET)
    assert last["preset_name"] == "reviewer"
    assert last["origin"] == "agent"
    assert datetime.fromisoformat(last["started_at"]) == T + timedelta(hours=4)
    assert body["marks"][0]["preset_id"] is None
    assert body["marks"][0]["preset_name"] is None
    assert datetime.fromisoformat(body["marks"][0]["ended_at"]) == ended


async def test_instants_are_served_in_utc(client, pool):
    await add_mark(pool, "z", T + timedelta(hours=1), T + timedelta(hours=2))
    (mark,) = (await client.get(SESSIONS, params=span(T, T + DAY))).json()["marks"]
    assert mark["started_at"].endswith(("Z", "+00:00"))
    assert mark["ended_at"].endswith(("Z", "+00:00"))


async def test_limit_truncates(client, pool):
    for hours in (1, 2, 3):
        await add_mark(pool, f"m{hours}", T + timedelta(hours=hours))
    two = (await client.get(SESSIONS, params=span(T, T + DAY, limit="2"))).json()
    assert slugs(two) == ["m1", "m2"]
    assert two["truncated"] is True
    three = (await client.get(SESSIONS, params=span(T, T + DAY, limit="3"))).json()
    assert slugs(three) == ["m1", "m2", "m3"]
    assert three["truncated"] is False


async def test_default_range_is_the_last_seven_days(client, pool):
    now = datetime.now(UTC)
    await add_mark(pool, "recent", now - timedelta(days=2), now - timedelta(days=1))
    await add_mark(pool, "old", now - timedelta(days=9), now - timedelta(days=8))
    await add_mark(pool, "running", now - timedelta(days=30))
    body = (await client.get(SESSIONS)).json()
    assert slugs(body) == ["running", "recent"]
    assert abs(datetime.fromisoformat(body["to"]) - now) < timedelta(minutes=1)
    assert datetime.fromisoformat(body["to"]) - datetime.fromisoformat(body["from"]) == timedelta(
        days=7
    )


async def test_empty_range(client):
    body = (await client.get(SESSIONS, params=span(T, T + DAY))).json()
    assert body["marks"] == []
    assert body["truncated"] is False


async def test_bad_params(client):
    for params in (
        span(T, T + DAY, limit="0"),
        span(T, T + DAY, limit="2001"),
        span(T, T + DAY, limit="x"),
        span(T + DAY, T),
        span(T, T),
        span(T, T + timedelta(days=101)),
        {"from": "yesterday"},
    ):
        response = await client.get(SESSIONS, params=params)
        assert response.status_code == 422, params
        assert response.json()["error"]["code"] == "invalid_request", params
    assert (await client.get(SESSIONS, params=span(T, T + DAY, limit="2000"))).status_code == 200


async def test_overlapping_fetches_one_more_than_the_limit(pool):
    for hours in (1, 2, 3):
        await add_mark(pool, f"m{hours}", T + timedelta(hours=hours))
    async with pool.connection() as conn:
        rows = await marks_db.overlapping(conn, T, T + DAY, 2)
    assert [r.slug for r in rows] == ["m1", "m2", "m3"]
    assert rows[0].ended_at is None


async def test_service_listener(settings, pool, client_service):
    anonymous = asgi_client(create_service_app(settings, pool, ["t"]), peer="10.9.9.9")
    async with anonymous as http:
        assert (await http.get(SESSIONS)).status_code == 401
    assert (await client_service.get(SESSIONS)).status_code == 200
