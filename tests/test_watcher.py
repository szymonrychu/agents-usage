"""The session marks watcher against a mocked orchestrator stream (no database)."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agents_usage import marks, watcher

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
A = uuid.UUID("11111111-1111-4111-8111-111111111111")
B = uuid.UUID("22222222-2222-4222-8222-222222222222")
C = uuid.UUID("33333333-3333-4333-8333-333333333333")
FIXTURE = Path(__file__).parent / "fixtures" / "stream-snapshot.sse"
STREAM = FIXTURE.read_bytes()
MESSAGE_BODY = "FIXTURE-MESSAGE-BODY"
SPEC_TEXT = "do the thing"
TOKEN = "sekret-token-value"
MAX_SECONDS = 60


def now() -> datetime:
    return NOW


def delay(attempt: int, jitter: float = 0.5) -> float:
    return watcher.next_delay(attempt, MAX_SECONDS, jitter)


class FakeRepo:
    """Keeps marks in memory with the same rules as the table and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.known: set[uuid.UUID] = set()
        self.open: set[uuid.UUID] = set()
        self.purges: list[datetime] = []
        self.purge_errors = 0

    async def reconcile(self, snapshot, at):
        self.calls.append(("reconcile", snapshot, at))
        plan = marks.plan_snapshot(self.open, self.known, snapshot)
        for item in plan.to_insert:
            self.known.add(item.session_id)
            self.open.add(item.session_id)
        self.open -= set(plan.to_close)

    async def insert(self, item):
        self.calls.append(("insert", item))
        fresh = item.session_id not in self.known
        self.known.add(item.session_id)
        if fresh:
            self.open.add(item.session_id)
        return fresh

    async def close(self, session_id, at):
        self.calls.append(("close", session_id, at))
        was_open = session_id in self.open
        self.open.discard(session_id)
        return was_open

    async def purge(self, older_than):
        self.purges.append(older_than)
        if self.purge_errors:
            self.purge_errors -= 1
            raise RuntimeError("database is down")
        return 0


@pytest.fixture
def fake_repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def token_file(tmp_path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    return path


@pytest.fixture
def wsettings(settings_for, token_file):
    return settings_for(
        "postgresql://u:p@db.invalid/usage",
        orchestrator_url="http://orch.test:8002/",
        orchestrator_token_file=str(token_file),
        reconnect_max_seconds=MAX_SECONDS,
    )


def streaming(content: bytes):
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


class Run:
    """Drives run_watcher; the injected sleep records delays and stops after `stop_after`."""

    def __init__(self, stop_after: int) -> None:
        self.stop = asyncio.Event()
        self.sleeps: list[float] = []
        self.stop_after = stop_after
        self.on_sleep = None

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))
        if len(self.sleeps) >= self.stop_after:
            self.stop.set()

    async def go(self, repo, settings, handler, **options) -> None:
        await asyncio.wait_for(
            watcher.run_watcher(
                None,
                settings,
                self.stop,
                transport=httpx.MockTransport(handler),
                now=now,
                sleep=self.sleep,
                jitter=lambda: 0.5,
                repo=repo,
                **options,
            ),
            timeout=5,
        )


def test_next_delay_grows_and_caps():
    delays = [watcher.next_delay(n, 60, 0.99) for n in range(11)]
    assert max(delays) <= 60
    assert delays == sorted(delays)
    assert delays[0] < 1.0
    assert delays[-1] == pytest.approx(60 * 0.995)
    assert watcher.next_delay(0, 60, 0) == 0.5
    assert watcher.next_delay(3, 60, 0) == 4.0
    assert watcher.next_delay(20, 60, 0) == 30.0
    assert watcher.next_delay(2, 60, 1.0) == 4.0


async def test_consume_snapshot_then_events(fake_repo, wsettings):
    seen = []

    def handler(request):
        seen.append(request)
        return streaming(STREAM)

    snapshots = []
    async with client_for(handler) as client:
        await watcher.consume(
            client, fake_repo, wsettings, "t", now, on_snapshot=lambda: snapshots.append(1)
        )
    assert [str(r.url) for r in seen] == ["http://orch.test:8002/api/v1/stream"]
    assert seen[0].headers["authorization"] == "Bearer t"
    assert seen[0].headers["accept"] == "text/event-stream"
    kinds = [call[0] for call in fake_repo.calls]
    assert kinds == ["reconcile", "insert", "close"]
    _, snapshot, at = fake_repo.calls[0]
    assert [m.session_id for m in snapshot] == [A, B]
    assert [m.origin for m in snapshot] == ["ui", "webhook"]
    assert at == NOW
    inserted = fake_repo.calls[1][1]
    assert (inserted.session_id, inserted.origin, inserted.preset_name) == (
        C,
        "schedule",
        "nightly",
    )
    assert inserted.started_at == datetime(2026, 11, 1, 10, 15, tzinfo=UTC)
    assert fake_repo.calls[2] == ("close", A, NOW)
    assert snapshots == [1]
    assert fake_repo.open == {B, C}


async def test_consume_unknown_removed_is_noop(fake_repo, wsettings):
    body = sse("session_removed", f'{{"session_id": "{C}"}}')
    async with client_for(lambda r: streaming(body)) as client:
        await watcher.consume(client, fake_repo, wsettings, "t", now)
    assert fake_repo.calls == [("close", C, NOW)]
    assert fake_repo.open == set()


@pytest.mark.parametrize(
    "body",
    [
        sse("session_removed", '{"session_id": "nope"}'),
        sse("session_removed", '{"session_id": 5}'),
        sse("session_removed", "{}"),
        sse("session_status", "{not json"),
        sse("session_status", "[1, 2]"),
        sse("session_status", '{"session": {"id": "nope"}}'),
        sse("snapshot", '{"sessions": "oops"}'),
        sse("snapshot", "{}"),
        sse("leader_changed", '{"session_id": "x"}'),
        sse("agent_status", '{"session_id": "x"}'),
        b"data: only data, no event name\n\n",
    ],
)
async def test_bad_events_are_skipped(fake_repo, wsettings, body):
    async with client_for(lambda r: streaming(body)) as client:
        await watcher.consume(client, fake_repo, wsettings, "t", now)
    assert fake_repo.calls == []


async def test_malformed_removal_does_not_stop_later_events(fake_repo, wsettings):
    body = sse("session_removed", '{"session_id": "nope"}') + sse(
        "session_removed", f'{{"session_id": "{A}"}}'
    )
    async with client_for(lambda r: streaming(body)) as client:
        await watcher.consume(client, fake_repo, wsettings, "t", now)
    assert fake_repo.calls == [("close", A, NOW)]


async def test_refused_status_raises_without_reading_the_body(fake_repo, wsettings):
    async with client_for(lambda r: httpx.Response(403, text="forbidden " + TOKEN)) as client:
        with pytest.raises(watcher.StreamRefused) as exc:
            await watcher.consume(client, fake_repo, wsettings, "t", now)
    assert exc.value.status == 403
    assert TOKEN not in str(exc.value)
    assert fake_repo.calls == []


async def test_unauthorized_backs_off_and_retries(fake_repo, wsettings):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401) if len(attempts) <= 2 else streaming(STREAM)

    run = Run(stop_after=3)
    await run.go(fake_repo, wsettings, handler)
    # two refusals back off 0.5-1 s then 1-2 s; the snapshot resets the counter, so the sleep
    # after the stream ends starts again from the first step
    assert run.sleeps == [delay(0), delay(1), delay(0)]
    assert len(attempts) == 3
    assert fake_repo.open == {B, C}


async def test_reconnect_after_eof(fake_repo, wsettings):
    second = sse("snapshot", '{"sessions": []}')
    bodies = [STREAM, second]

    def handler(request):
        return streaming(bodies.pop(0))

    fake_repo.known.add(A)  # A is still open from before the first connection
    fake_repo.open.add(A)
    run = Run(stop_after=2)
    await run.go(fake_repo, wsettings, handler)
    assert bodies == []
    kinds = [call[0] for call in fake_repo.calls]
    assert kinds == ["reconcile", "insert", "close", "reconcile"]
    # the second snapshot is empty: every mark still open is closed at the reconnect
    assert fake_repo.open == set()
    assert fake_repo.calls[-1][1] == []


async def test_read_timeout_reconnects(fake_repo, wsettings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("silent", request=request)
        return streaming(STREAM)

    run = Run(stop_after=2)
    await run.go(fake_repo, wsettings, handler)
    assert len(calls) == 2
    assert run.sleeps == [delay(0), delay(0)]
    assert fake_repo.open == {B, C}


async def test_token_file_read_per_connect(tmp_path, fake_repo, wsettings, token_file):
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return streaming(b"")

    run = Run(stop_after=2)
    run.on_sleep = lambda n: token_file.write_text("rotated\n") if n == 1 else None
    await run.go(fake_repo, wsettings, handler)
    assert seen == [f"Bearer {TOKEN}", "Bearer rotated"]


@pytest.mark.parametrize("content", [None, ""])
async def test_missing_or_empty_token_file_backs_off_without_a_request(
    fake_repo, wsettings, token_file, content, caplog
):
    if content is None:
        token_file.unlink()
    else:
        token_file.write_text(content)
    seen = []
    run = Run(stop_after=2)
    with caplog.at_level(logging.INFO):
        await run.go(fake_repo, wsettings, lambda r: seen.append(r) or streaming(b""))
    assert seen == []
    assert run.sleeps == [delay(0), delay(1)]
    assert "orchestrator token file unreadable or empty" in caplog.text


async def test_token_never_logged(fake_repo, wsettings, caplog):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401, text=TOKEN) if len(attempts) == 1 else streaming(STREAM)

    run = Run(stop_after=2)
    with caplog.at_level(logging.DEBUG):
        await run.go(fake_repo, wsettings, handler)
    assert "stream refused status=401" in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in (record.exc_text or "")
        assert TOKEN not in repr(record.args)


async def test_message_text_never_logged_or_stored(fake_repo, wsettings, caplog):
    run = Run(stop_after=1)
    with caplog.at_level(logging.DEBUG):
        await run.go(fake_repo, wsettings, lambda r: streaming(STREAM))
    assert fake_repo.calls
    for text in (MESSAGE_BODY, SPEC_TEXT):
        assert text not in caplog.text
        assert text not in repr(fake_repo.calls)


async def test_unexpected_error_is_logged_and_retried(fake_repo, wsettings, caplog):
    async def broken(snapshot, at):
        raise RuntimeError("database is down")

    fake_repo.reconcile = broken
    run = Run(stop_after=2)
    with caplog.at_level(logging.WARNING):
        await run.go(fake_repo, wsettings, lambda r: streaming(STREAM))
    assert run.sleeps == [delay(0), delay(1)]
    assert "RuntimeError: database is down" in caplog.text


async def test_stop_ends_a_stream_that_never_ends(fake_repo, wsettings):
    reading = asyncio.Event()

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse("snapshot", '{"sessions": []}')
            reading.set()
            await asyncio.Event().wait()

    run = Run(stop_after=99)

    async def stopper():
        await reading.wait()
        run.stop.set()

    task = asyncio.create_task(stopper())
    await run.go(fake_repo, wsettings, lambda r: httpx.Response(200, stream=Endless()))
    await task
    assert run.sleeps == []
    assert [call[0] for call in fake_repo.calls] == ["reconcile"]


async def test_purge_loop_hourly_in_its_own_try(fake_repo, wsettings):
    fake_repo.purge_errors = 1
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.purge_loop(fake_repo, wsettings, stop, now, every=0.01))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(fake_repo.purges) >= 3  # the first failed, the loop went on
    assert set(fake_repo.purges) == {NOW - timedelta(days=wsettings.retention_days)}


async def test_run_watcher_purges_while_connected(fake_repo, wsettings):
    run = Run(stop_after=1)
    await run.go(fake_repo, wsettings, lambda r: streaming(b""))
    assert fake_repo.purges == [NOW - timedelta(days=90)]
