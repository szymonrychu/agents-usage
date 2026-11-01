"""The lazy probe: run claude once when a reader asks and every reading is old.

There is no timer. The prober waits on the `agents_usage_probe` channel (the api notifies it
from the service listener's `current`), handles one request at a time, and probes only when
`probe_due` says so. The OAuth token is read from its file on every probe, passed only to the
claude child, never logged, and redacted from every stored text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agents_common.db import listen
from psycopg_pool import AsyncConnectionPool

from agents_usage import runner
from agents_usage.db import probes
from agents_usage.db.probes import PROBE_CHANNEL, ProbeRecord
from agents_usage.parser import parse_stream
from agents_usage.runner import CliRun
from agents_usage.settings import Settings

log = logging.getLogger(__name__)

REDACTED = "[redacted]"
DETAIL_LIMIT = 2000
# A cut-off piece of the token shorter than this is not redacted at the start of a text.
MIN_PARTIAL = 4
TOKEN_UNREADABLE = "claude token file unreadable or empty"
PRUNE_EVERY_SECONDS = 3600.0

Run = Callable[[list[str], dict[str, str], str, float], Awaitable[CliRun]]
Handle = Callable[[AsyncConnectionPool, Settings], Awaitable[bool]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _scrub(text: str, secret: str) -> str:
    """`text` with every `secret` replaced by `[redacted]`. stderr is kept from its tail, so a
    text may also start with the end of the secret: that piece is redacted too."""
    if not secret:
        return text
    text = text.replace(secret, REDACTED)
    for size in range(len(secret) - 1, MIN_PARTIAL - 1, -1):
        if text.startswith(secret[-size:]):
            return REDACTED + text[size:]
    return text


def redact(text: str, secret: str, limit: int = DETAIL_LIMIT) -> str:
    """`text` with the secret redacted, capped at `limit` characters (the tail is kept). An
    empty secret leaves the text as it is."""
    text = _scrub(text, secret)
    return text[-limit:] if len(text) > limit else text


def _redact_json(value: Any, secret: str) -> Any:
    """A JSON value with the secret replaced in every string, keys included, uncapped."""
    if isinstance(value, str):
        return value.replace(secret, REDACTED) if secret else value
    if isinstance(value, list):
        return [_redact_json(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact_json(k, secret): _redact_json(v, secret) for k, v in value.items()}
    return value


def _detail(text: str | None, secret: str) -> str | None:
    return redact(text, secret) if text else None


def to_record(started_at: datetime, observed_at: datetime, run: CliRun, token: str) -> ProbeRecord:
    """Classify one claude run: timeout, then a rate limit event (ok, whatever the exit code),
    then an error result line, then a non-zero exit, else no rate limit event."""
    parsed = parse_stream(run.stdout)
    error: str | None
    detail: str | None = None
    if run.timed_out:
        error, detail = "timeout", _detail(run.stderr, token)
    elif parsed.event is not None:
        error = None
    elif parsed.is_error:
        error, detail = "cli_error", _detail(parsed.result_text, token)
    elif run.returncode != 0:
        error, detail = "cli_exit", _detail(run.stderr, token)
    else:
        error = "no_rate_limit_event"
    cli_version = parsed.cli_version
    return ProbeRecord(
        started_at=started_at,
        observed_at=observed_at,
        ok=error is None,
        error=error,
        detail=detail,
        cli_version=None if cli_version is None else redact(cli_version, token),
        cost_usd=parsed.cost_usd,
        duration_ms=max(run.duration_ms, 0),
        raw=None if parsed.event is None else _redact_json(parsed.event, token),
        windows=parsed.windows if error is None else {},
    )


def _read_token(path: str | None) -> str:
    """The token file's content, stripped; empty when unset, unreadable or empty."""
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _summary(rec: ProbeRecord) -> str:
    """The log line of a probe: never the detail (stderr), except the fixed token-file text."""
    if rec.detail == TOKEN_UNREADABLE:
        return f"failed: {rec.error}: {TOKEN_UNREADABLE}"
    if not rec.ok:
        return f"failed: {rec.error} ({rec.duration_ms} ms)"
    windows = " ".join(f"{w.metric}={w.percent}%" for w in rec.windows.values()) or "no windows"
    return f"ok: {windows} cost_usd={rec.cost_usd} ({rec.duration_ms} ms)"


async def probe_once(
    pool: AsyncConnectionPool,
    settings: Settings,
    *,
    run: Run = runner.run_cli,
    now: Callable[[], datetime] = _utcnow,
) -> int:
    """Run one probe and store the attempt, whatever its outcome; returns the probe id."""
    started_at = now()
    token = _read_token(settings.claude_token_file)
    if not token:
        rec = ProbeRecord(
            started_at, now(), False, "cli_error", TOKEN_UNREADABLE, None, None, 0, None, {}
        )
    else:
        began = time.monotonic()
        try:
            result = await run(
                runner.probe_argv(settings),
                runner.probe_env(settings, token),
                settings.claude_home,
                settings.probe_timeout_seconds,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - began) * 1000)
            result = CliRun(None, "", f"{type(exc).__name__}: {exc}", elapsed, False)
        rec = to_record(started_at, now(), result, token)
    async with pool.connection() as conn:
        probe_id = await probes.insert_probe(conn, rec)
    log.info("probe %d %s", probe_id, _summary(rec))
    return probe_id


async def handle_request(
    pool: AsyncConnectionPool,
    settings: Settings,
    *,
    run: Run = runner.run_cli,
    now: Callable[[], datetime] = _utcnow,
) -> bool:
    """Probe when due (every reading and the last attempt are old enough); whether it did."""
    async with pool.connection() as conn:
        due = await probes.probe_due(
            conn, now(), settings.probe_after_seconds, settings.probe_min_interval_seconds
        )
    if not due:
        return False
    await probe_once(pool, settings, run=run, now=now)
    return True


async def _prune(pool: AsyncConnectionPool, settings: Settings) -> None:
    older_than = _utcnow() - timedelta(days=settings.retention_days)
    try:
        async with pool.connection() as conn:
            removed = await probes.prune(conn, older_than)
    except Exception:
        log.exception("pruning old probes failed")
        return
    if removed:
        log.info("pruned %d old probes", removed)


async def run_prober(
    pool: AsyncConnectionPool,
    settings: Settings,
    stop: asyncio.Event,
    *,
    requests: AsyncIterator[str] | None = None,
    handle: Handle = handle_request,
) -> None:
    """Handle probe requests one at a time until `stop` is set.

    Requests that arrive while one is handled are coalesced into one more call. A failing
    `handle` is logged and the loop goes on; a failing `requests` stream ends the loop with its
    error. After a probe, old probes are pruned at most hourly.
    """
    if requests is None:
        requests = listen(settings.database_url.get_secret_value(), PROBE_CHANNEL)
    pending = asyncio.Event()

    async def receive() -> None:
        async for _ in requests:
            pending.set()

    reader = asyncio.create_task(receive())
    stopping = asyncio.create_task(stop.wait())
    last_prune: float | None = None
    log.info("waiting for probe requests on %s", PROBE_CHANNEL)
    try:
        while True:
            waiting = asyncio.create_task(pending.wait())
            await asyncio.wait({waiting, stopping, reader}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if stop.is_set():
                return
            if not pending.is_set():
                reader.result()  # the stream ended: re-raise its error, if any
                return
            pending.clear()
            try:
                probed = await handle(pool, settings)
            except Exception:
                log.exception("probe request failed")
                continue
            if probed and (
                last_prune is None or time.monotonic() - last_prune >= PRUNE_EVERY_SECONDS
            ):
                last_prune = time.monotonic()
                await _prune(pool, settings)
    finally:
        for task in (reader, stopping):
            task.cancel()
        for task in (reader, stopping):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
