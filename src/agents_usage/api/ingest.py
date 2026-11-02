"""POST /usage/v1/samples: the agents push their statusLine `rate_limits` here.

Neither the body nor the bearer token is ever logged. `observed_at` is the server's receive
time, never a client clock; `raw` is the posted `rate_limits` verbatim.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.errors import DataError
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from agents_usage.api.errors import ApiError
from agents_usage.api.schemas import SampleIn
from agents_usage.db import samples
from agents_usage.parser import windows_from_statusline

log = logging.getLogger(__name__)

SAMPLE_MAX_BYTES = 8192
PRUNE_EVERY = timedelta(hours=1)

router = APIRouter()


class SampleStore(Protocol):
    async def insert(self, rec: samples.SampleRecord) -> int: ...

    async def prune(self, older_than: datetime) -> int: ...


class PoolSampleStore:
    """The database-backed store: one pooled connection per call."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def insert(self, rec: samples.SampleRecord) -> int:
        async with self.pool.connection() as conn:
            return await samples.insert_sample(conn, rec)

    async def prune(self, older_than: datetime) -> int:
        async with self.pool.connection() as conn:
            return await samples.prune(conn, older_than)


class IngestState:
    """What the ingest router needs from `app.state`: the store, the clock and the prune mark."""

    def __init__(
        self, store: SampleStore, retention: timedelta, now: Callable[[], datetime] | None = None
    ) -> None:
        self.store = store
        self.retention = retention
        self.now = now or (lambda: datetime.now(UTC))
        self.last_prune: datetime | None = None


async def _read_body(request: Request) -> bytes:
    """The body, refused with 413 as soon as it exceeds SAMPLE_MAX_BYTES (streamed, so a
    missing or lying Content-Length does not help)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > SAMPLE_MAX_BYTES:
        raise ApiError(413, "too_large", f"body exceeds {SAMPLE_MAX_BYTES} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > SAMPLE_MAX_BYTES:
            raise ApiError(413, "too_large", f"body exceeds {SAMPLE_MAX_BYTES} bytes")
    return bytes(body)


def _parse(body: bytes) -> SampleIn:
    try:
        return SampleIn.model_validate_json(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise ApiError(422, "invalid_request", f"{where or 'body'}: {first['msg']}") from None


async def _prune_if_due(state: IngestState, now: datetime) -> None:
    """Prune at most hourly, after an insert; a failure is logged and never fails the request."""
    if state.last_prune is not None and now - state.last_prune < PRUNE_EVERY:
        return
    state.last_prune = now
    try:
        removed = await state.store.prune(now - state.retention)
    except Exception:
        log.exception("pruning old samples failed")
        return
    if removed:
        log.info("pruned %d old samples", removed)


@router.post("/samples", status_code=202)
async def post_sample(request: Request) -> JSONResponse:
    state: IngestState = request.app.state.ingest
    sample = _parse(await _read_body(request))
    now = state.now()
    windows = windows_from_statusline(sample.rate_limits, now)
    if not windows:
        raise ApiError(422, "no_windows", "rate_limits holds no usable window")
    try:
        json.dumps(sample.rate_limits, allow_nan=False)
    except ValueError:
        raise ApiError(422, "invalid_request", "rate_limits: non-finite number") from None
    record = samples.SampleRecord(
        now, sample.agent, sample.cli_version, sample.rate_limits, windows
    )
    try:
        await state.store.insert(record)
    except DataError:
        raise ApiError(422, "invalid_request", "rate_limits: not storable") from None
    await _prune_if_due(state, now)
    return JSONResponse({"windows": len(windows)}, status_code=202)
