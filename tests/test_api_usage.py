"""The read routes over a migrated database (needs Postgres)."""

import math
import random
import re
from datetime import UTC, datetime, timedelta

from conftest import SERVICE_TOKEN, asgi_client

from agents_usage.api.app import PREFIX, create_service_app
from agents_usage.api.ranges import bucket_seconds
from agents_usage.db import probes, readings, samples
from agents_usage.parser import Window

CURRENT = f"{PREFIX}/current"
HISTORY = f"{PREFIX}/history"


def now_s() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


async def add_sample(pool, at, agent="agent-x-0", **windows):
    """windows: metric=(percent, resets_at)."""
    rec = samples.SampleRecord(
        at, agent, "2.1.304", {}, {m: Window(m, p, r) for m, (p, r) in windows.items()}
    )
    async with pool.connection() as conn:
        await samples.insert_sample(conn, rec)


async def add_probe(pool, at, ok=True, **windows):
    rec = probes.ProbeRecord(
        at,
        at,
        ok,
        None if ok else "timeout",
        None,
        "2.1.304",
        None,
        1000,
        None,
        {m: Window(m, p, r) for m, (p, r) in windows.items()} if ok else {},
    )
    async with pool.connection() as conn:
        await probes.insert_probe(conn, rec)


async def test_current_shape(client):
    response = await client.get(CURRENT)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"windows", "last_sample", "last_probe"}
    assert body == {"windows": {}, "last_sample": None, "last_probe": None}


async def test_current_reports_windows_sample_and_probe(client, pool):
    now = now_s()
    resets = now + timedelta(days=2)
    await add_sample(pool, now - timedelta(seconds=30), "agent-a", seven_day=(41.5, resets))
    await add_probe(pool, now - timedelta(seconds=10), ok=False)
    body = (await client.get(CURRENT)).json()
    window = body["windows"]["seven_day"]
    assert set(window) == {"percent", "resets_at", "observed_at", "stale", "source"}
    assert window["percent"] == 41.5
    assert window["stale"] is False
    assert parse(window["resets_at"]) == resets
    assert body["last_sample"]["agent"] == "agent-a"
    assert parse(body["last_sample"]["at"]) == now - timedelta(seconds=30)
    assert body["last_probe"] == {"at": body["last_probe"]["at"], "ok": False, "error": "timeout"}


async def test_current_newest_reading_wins_in_a_window(client, pool):
    now = now_s()
    resets = now + timedelta(days=3)
    await add_sample(pool, now - timedelta(seconds=120), "agent-a", seven_day=(30.0, resets))
    await add_sample(pool, now - timedelta(seconds=60), "agent-b", seven_day=(25.0, resets))
    window = (await client.get(CURRENT)).json()["windows"]["seven_day"]
    assert window["percent"] == 25.0
    assert parse(window["observed_at"]) == now - timedelta(seconds=60)
    assert window["source"] == "statusline"


async def test_current_early_reset_lowers_percent_in_same_window(client, pool):
    now = now_s()
    resets = now + timedelta(days=3)
    await add_sample(pool, now - timedelta(hours=3), seven_day=(92.0, resets))
    await add_sample(pool, now - timedelta(minutes=1), seven_day=(34.0, resets))
    window = (await client.get(CURRENT)).json()["windows"]["seven_day"]
    assert window["percent"] == 34.0


async def test_current_new_window_wins_after_reset(client, pool):
    now = now_s()
    await add_sample(pool, now - timedelta(hours=2), five_hour=(95.0, now + timedelta(minutes=30)))
    await add_sample(pool, now - timedelta(seconds=10), five_hour=(3.0, now + timedelta(hours=5)))
    assert (await client.get(CURRENT)).json()["windows"]["five_hour"]["percent"] == 3.0


async def test_current_drops_a_reset_window(client, pool):
    now = now_s()
    await add_sample(pool, now - timedelta(hours=2), five_hour=(95.0, now - timedelta(hours=1)))
    assert (await client.get(CURRENT)).json()["windows"] == {}


async def test_newest_at_ignores_a_probe_without_windows(pool):
    now = now_s()
    resets = now + timedelta(days=1)
    async with pool.connection() as conn:
        assert await readings.newest_at(conn) is None
    await add_probe(pool, now - timedelta(minutes=5))  # ok, but the parser left no windows
    await add_probe(pool, now - timedelta(minutes=1), ok=False)
    async with pool.connection() as conn:
        assert await readings.newest_at(conn) is None
    await add_probe(pool, now - timedelta(hours=1), seven_day=(20.0, resets))
    await add_probe(pool, now - timedelta(seconds=30))
    async with pool.connection() as conn:
        assert await readings.newest_at(conn) == now - timedelta(hours=1)
        cur = await conn.execute("SELECT max(observed_at) FROM readings")
        assert (await cur.fetchone())[0] == now - timedelta(hours=1)
    await add_sample(pool, now - timedelta(minutes=2), seven_day=(21.0, resets))
    async with pool.connection() as conn:
        assert await readings.newest_at(conn) == now - timedelta(minutes=2)


async def test_current_mixes_sources(client, pool):
    now = now_s()
    resets = now + timedelta(days=1)
    await add_sample(pool, now - timedelta(seconds=300), seven_day=(20.0, resets))
    await add_probe(pool, now - timedelta(seconds=30), seven_day=(21.0, resets))
    window = (await client.get(CURRENT)).json()["windows"]["seven_day"]
    assert window["source"] == "probe"
    assert window["percent"] == 21.0


async def test_current_stale(client, pool):
    now = now_s()
    await add_sample(pool, now - timedelta(seconds=1900), seven_day=(5.0, now + timedelta(days=1)))
    assert (await client.get(CURRENT)).json()["windows"]["seven_day"]["stale"] is True


async def test_history_order_and_filter(client, pool):
    now = now_s()
    for hours, five, seven in ((3, 10.0, 20.0), (2, 11.0, 21.0), (1, 12.0, 22.0)):
        await add_sample(
            pool,
            now - timedelta(hours=hours),
            five_hour=(five, now + timedelta(hours=3)),
            seven_day=(seven, now + timedelta(days=3)),
        )
    body = (await client.get(HISTORY, params={"metric": "seven_day"})).json()
    assert set(body) == {"from", "to", "bucket_seconds", "points"}
    assert [p["percent"] for p in body["points"]] == [20.0, 21.0, 22.0]
    assert {p["metric"] for p in body["points"]} == {"seven_day"}
    assert {p["source"] for p in body["points"]} == {"statusline"}
    assert set(body["points"][0]) == {"observed_at", "metric", "percent", "resets_at", "source"}
    both = (await client.get(HISTORY)).json()["points"]
    assert len(both) == 6
    times = [parse(p["observed_at"]) for p in both]
    assert times == sorted(times)


async def test_history_downsampled_keeps_peak(client, pool):
    rng = random.Random(7)
    frm = now_s() - timedelta(days=2)
    to = frm + timedelta(days=2)
    resets = to + timedelta(days=5)
    rows = []
    for i in range(1000):
        at = frm + timedelta(seconds=0.5 + 172 * i)
        rows.append((at, round(rng.uniform(0, 99), 1), round(rng.uniform(0, 99), 1)))
    for at, five, seven in rows:
        await add_sample(pool, at, five_hour=(five, resets), seven_day=(seven, resets))
    response = await client.get(
        HISTORY,
        params={"from": frm.isoformat(), "to": to.isoformat(), "max_points": 100},
    )
    body = response.json()
    assert body["bucket_seconds"] == math.ceil(172800 / 100)
    width = body["bucket_seconds"]
    for index, metric in ((1, "five_hour"), (2, "seven_day")):
        points = [p for p in body["points"] if p["metric"] == metric]
        assert 0 < len(points) <= 100
        best: dict[int, tuple[float, datetime]] = {}
        for row in rows:
            bucket = int((row[0] - frm).total_seconds() // width)
            if bucket not in best or (row[index], row[0]) >= best[bucket]:
                best[bucket] = (row[index], row[0])
        got = {(p["percent"], parse(p["observed_at"])) for p in points}
        assert got == set(best.values())


async def test_history_few_readings_unchanged(client, pool):
    now = now_s()
    for minutes in (50, 40, 30, 20, 10):
        await add_sample(pool, now - timedelta(minutes=minutes), five_hour=(minutes, now))
    params = {"from": (now - timedelta(hours=24)).isoformat(), "to": now.isoformat()}
    points = (await client.get(HISTORY, params=params)).json()["points"]
    assert len(points) == 5


async def test_history_bad_params(client):
    now = now_s()
    for params in (
        {"max_points": 9},
        {"max_points": 1001},
        {"from": now.isoformat(), "to": (now - timedelta(hours=1)).isoformat()},
        {"from": now.isoformat(), "to": now.isoformat()},
        {"from": (now - timedelta(days=101)).isoformat(), "to": now.isoformat()},
        {"from": "yesterday"},
        {"metric": "Seven Day"},
    ):
        response = await client.get(HISTORY, params=params)
        assert response.status_code == 422, params
        assert response.json()["error"]["code"] == "invalid_request", params
    ok = await client.get(
        HISTORY,
        params={"from": (now - timedelta(days=100)).isoformat(), "to": now.isoformat()},
    )
    assert ok.status_code == 200


async def test_history_range_filter(client, pool):
    now = now_s()
    frm, to = now - timedelta(hours=2), now - timedelta(hours=1)
    resets = now + timedelta(days=1)
    for offset, percent in (
        (timedelta(hours=3), 1.0),
        (timedelta(hours=2), 2.0),
        (timedelta(minutes=90), 3.0),
        (timedelta(hours=1), 4.0),
        (timedelta(minutes=10), 5.0),
    ):
        await add_sample(pool, now - offset, five_hour=(percent, resets))
    params = {"from": frm.isoformat(), "to": to.isoformat()}
    points = (await client.get(HISTORY, params=params)).json()["points"]
    assert [p["percent"] for p in points] == [2.0, 3.0]


async def test_history_default_range(client, pool):
    now = now_s()
    resets = now + timedelta(days=1)
    await add_sample(pool, now - timedelta(days=8), five_hour=(1.0, resets))
    await add_sample(pool, now - timedelta(days=6), five_hour=(2.0, resets))
    body = (await client.get(HISTORY)).json()
    assert [p["percent"] for p in body["points"]] == [2.0]
    assert parse(body["to"]) - parse(body["from"]) == timedelta(days=7)


def fill(conn) -> None:
    """Many samples and probes over 900 days, so the planner has a reason to use the indexes."""
    conn.execute(
        "INSERT INTO samples (observed_at, agent, raw)"
        " SELECT now() - make_interval(secs => g * 1900), 'agent-x-0', '{}'::jsonb"
        " FROM generate_series(1, 40000) g"
    )
    conn.execute(
        "INSERT INTO sample_windows (sample_id, metric, percent, resets_at)"
        " SELECT s.id, m, 10, s.observed_at + interval '1 day' FROM samples s,"
        " unnest(ARRAY['five_hour', 'seven_day']) m"
    )
    conn.execute(
        "INSERT INTO probes (started_at, observed_at, ok, duration_ms)"
        " SELECT t, t, true, 1000 FROM (SELECT now() - make_interval(secs => g * 17000) AS t"
        " FROM generate_series(1, 4000) g) x"
    )
    conn.execute(
        "INSERT INTO probe_windows (probe_id, metric, percent, resets_at)"
        " SELECT p.id, 'seven_day', 10, p.observed_at + interval '1 day' FROM probes p"
    )
    for table in ("samples", "sample_windows", "probes", "probe_windows"):
        conn.execute(f"ANALYZE {table}")


# A plain, index-only or bitmap scan of one of the observed_at indexes.
INDEX_SCAN = r"(Scan using|Bitmap Index Scan on) (samples_observed_at_idx|probes_observed_at_idx)"


def test_history_index_scan(conn):
    fill(conn)
    now = datetime.now(UTC)
    frm = now - timedelta(days=90)
    params = {"metric": None, "frm": frm, "to": now, "width": bucket_seconds(frm, now, 500)}
    plan = "\n".join(
        row[0] for row in conn.execute("EXPLAIN " + readings.HISTORY_SQL, params).fetchall()
    )
    assert not re.search(r"Seq Scan on (samples|probes)\b", plan), plan
    assert re.search(INDEX_SCAN, plan), plan


def test_current_queries_use_the_indexes(conn):
    fill(conn)
    now = datetime.now(UTC)
    for sql, params in (
        (readings.NEWEST_SQL, None),
        (readings.WINDOW_GROUPS_SQL, {"since": now - timedelta(days=8), "now": now}),
    ):
        plan = "\n".join(row[0] for row in conn.execute("EXPLAIN " + sql, params).fetchall())
        assert not re.search(r"Seq Scan on (samples|probes)\b", plan), plan
        if sql is readings.NEWEST_SQL:
            assert re.search(INDEX_SCAN, plan), plan


async def test_service_listener(settings, pool, client_service):
    anonymous = asgi_client(create_service_app(settings, pool, [SERVICE_TOKEN]), peer="10.9.9.9")
    async with anonymous as http:
        assert (await http.get(CURRENT)).status_code == 401
        assert (await http.get(HISTORY)).status_code == 401
    assert (await client_service.get(CURRENT)).status_code == 200
    assert (await client_service.get(HISTORY)).status_code == 200
