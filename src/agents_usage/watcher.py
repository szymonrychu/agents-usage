"""The session marks watcher: one `session_marks` row per orchestrator session.

It reads the orchestrator's global SSE stream (`GET /api/v1/stream` on the service port, bearer
service token) and keeps only the mark fields of `snapshot`, `session_status` and
`session_removed`. Every other event, and every other field of those events, is dropped on
parse: the stream carries message text and prompts, which must reach neither the database nor
a log. The token is read from its file on every connection and never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from psycopg_pool import AsyncConnectionPool

from agents_usage import marks
from agents_usage.db.marks import MarksRepo
from agents_usage.marks import Mark
from agents_usage.settings import Settings
from agents_usage.sse import SseEvent, SseParser

log = logging.getLogger(__name__)

STREAM_PATH = "/api/v1/stream"
PURGE_EVERY_SECONDS = 3600.0
CONNECT_TIMEOUT_SECONDS = 10.0
TOKEN_UNREADABLE = "orchestrator token file unreadable or empty"


class Repo(Protocol):
    async def reconcile(self, snapshot: list[Mark], now: datetime) -> None: ...
    async def insert(self, mark: Mark) -> bool: ...
    async def close(self, session_id: uuid.UUID, now: datetime) -> bool: ...
    async def purge(self, older_than: datetime) -> int: ...


Clock = Callable[[], datetime]


class StreamRefused(Exception):
    """The orchestrator answered the stream request with a non-2xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"stream refused status={status}")
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(UTC)


def next_delay(attempt: int, max_seconds: float, jitter: float) -> float:
    """Seconds to wait before reconnect number `attempt` (from 0): 1 s doubling up to
    `max_seconds`, scaled by 0.5..1 with `jitter` in [0, 1)."""
    return min(max_seconds, 2.0**attempt) * (0.5 + jitter / 2)


def _json(event: SseEvent) -> dict[str, Any] | None:
    try:
        data = json.loads(event.data)
    except ValueError:
        log.warning("skipped %s event: data is not JSON", event.event)
        return None
    return data if isinstance(data, dict) else None


async def _handle(
    event: SseEvent, repo: Repo, now: Clock, on_snapshot: Callable[[], None] | None
) -> None:
    """Apply one stream event; only snapshot, session_status and session_removed matter."""
    if event.event not in ("snapshot", "session_status", "session_removed"):
        return
    data = _json(event)
    if data is None:
        return
    if event.event == "snapshot":
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            log.warning("skipped snapshot: no session list")
            return  # an empty list would close every open mark
        entries = [e.get("session") if isinstance(e, dict) else None for e in sessions]
        found = [marks.mark_from_session(view) for view in entries]
        snapshot = [m for m in found if m is not None]
        if len(snapshot) != len(found):
            log.warning("snapshot: skipped %d malformed sessions", len(found) - len(snapshot))
        await repo.reconcile(snapshot, now())
        log.info("snapshot: %d sessions", len(snapshot))
        if on_snapshot is not None:
            on_snapshot()
    elif event.event == "session_status":
        mark = marks.mark_from_session(data.get("session"))
        if mark is not None:
            await repo.insert(mark)
    else:
        raw = data.get("session_id")
        try:
            session_id = uuid.UUID(raw) if isinstance(raw, str) else None
        except ValueError:
            session_id = None
        if session_id is None:
            log.warning("skipped session_removed: malformed session id")
            return
        await repo.close(session_id, now())


async def consume(
    client: httpx.AsyncClient,
    repo: Repo,
    settings: Settings,
    token: str,
    now: Clock,
    *,
    on_snapshot: Callable[[], None] | None = None,
) -> None:
    """Read one connection of the stream until it ends (returns) or fails (raises).
    `on_snapshot` is called after each snapshot has been reconciled."""
    url = f"{(settings.orchestrator_url or '').rstrip('/')}{STREAM_PATH}"
    timeout = httpx.Timeout(CONNECT_TIMEOUT_SECONDS, read=settings.stream_read_timeout_seconds)
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        if not 200 <= response.status_code < 300:
            raise StreamRefused(response.status_code)  # the body is never read
        log.info("stream connected")
        parser = SseParser()
        async for line in response.aiter_lines():
            event = parser.feed(line)
            if event is not None:
                await _handle(event, repo, now, on_snapshot)


def _read_token(path: str | None) -> str:
    """The token file's content, stripped; empty when unset, unreadable or empty."""
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _describe(exc: BaseException) -> str:
    if isinstance(exc, StreamRefused):
        return str(exc)
    return f"stream failed: {type(exc).__name__}: {exc}"


async def purge_loop(
    repo: Repo,
    settings: Settings,
    stop: asyncio.Event,
    now: Clock = _utcnow,
    *,
    every: float = PURGE_EVERY_SECONDS,
) -> None:
    """Delete the marks closed longer ago than the retention, now and then every `every`
    seconds, until `stop`. A failure is logged and never ends the loop."""
    while not stop.is_set():
        try:
            removed = await repo.purge(now() - timedelta(days=settings.retention_days))
        except Exception:
            log.exception("purging old session marks failed")
        else:
            if removed:
                log.info("purged %d old session marks", removed)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def _until_stop(work: Awaitable[None], stop: asyncio.Event) -> None:
    """Await `work`, but cancel it when `stop` is set; its exception propagates."""
    task = asyncio.ensure_future(work)
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    if task.done() and not task.cancelled():
        task.result()


async def run_watcher(
    pool: AsyncConnectionPool | None,
    settings: Settings,
    stop: asyncio.Event,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Clock = _utcnow,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    jitter: Callable[[], float] = random.random,
    repo: Repo | None = None,
    purge_every: float = PURGE_EVERY_SECONDS,
) -> None:
    """Keep one connection to the stream until `stop`: reconnect with a jittered exponential
    backoff after any end or failure, the counter back to 0 after each snapshot."""
    if repo is None:
        assert pool is not None
        repo = MarksRepo(pool)
    wait = sleep or (lambda seconds: _sleep_or_stop(stop, seconds))
    attempt = 0

    def snapshot_done() -> None:
        nonlocal attempt
        attempt = 0

    purging = asyncio.create_task(purge_loop(repo, settings, stop, now, every=purge_every))
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            while not stop.is_set():
                token = _read_token(settings.orchestrator_token_file)
                try:
                    if not token:
                        log.warning(TOKEN_UNREADABLE)
                    else:
                        await _until_stop(
                            consume(client, repo, settings, token, now, on_snapshot=snapshot_done),
                            stop,
                        )
                        if not stop.is_set():
                            log.info("stream ended")
                except Exception as exc:
                    log.warning("%s", _describe(exc))
                if stop.is_set():
                    break
                await wait(next_delay(attempt, settings.reconnect_max_seconds, jitter()))
                attempt += 1
    finally:
        purging.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purging
