"""GET /usage/v1/current and /usage/v1/history: what the usage gate and the web UI read.

`current` is also what wakes the prober: on the service listener only (the orchestrator's gate
asking is "something needs the number"; the web UI looking at a page is not), it sends a NOTIFY
when the newest reading is overdue. The notification is fire and forget and never delays or fails
the answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from agents_common.db import notify
from fastapi import APIRouter, FastAPI, Query, Request
from psycopg_pool import AsyncConnectionPool

from agents_usage.api.errors import ApiError
from agents_usage.api.ranges import bucket_seconds, resolve_range
from agents_usage.api.schemas import (
    CurrentView,
    HistoryView,
    LastProbeView,
    LastSampleView,
    PointView,
    WindowView,
)
from agents_usage.db import probes, readings, samples
from agents_usage.db.probes import PROBE_CHANNEL, ProbeRow
from agents_usage.db.readings import WindowGroup
from agents_usage.db.samples import SampleRow

log = logging.getLogger(__name__)

METRIC_PATTERN = r"^[a-z0-9_]{1,64}$"

router = APIRouter()


def current_view(
    groups: list[WindowGroup],
    last_sample: SampleRow | None,
    last_probe: ProbeRow | None,
    now: datetime,
    stale_seconds: int,
) -> CurrentView:
    """The answer of `current`. Per metric the newest window (the latest reset) is reported and
    a window that has reset (`resets_at <= now`) is dropped: usage inside a window only grows,
    so the highest reading of the newest window is the current one. `stale` is decided here, once;
    the orchestrator trusts it."""
    newest: dict[str, WindowGroup] = {}
    for group in groups:
        if group.resets_at <= now:
            continue
        held = newest.get(group.metric)
        if held is None or group.resets_at > held.resets_at:
            newest[group.metric] = group
    limit = timedelta(seconds=stale_seconds)
    return CurrentView(
        windows={
            metric: WindowView(
                percent=group.percent,
                resets_at=group.resets_at,
                observed_at=group.observed_at,
                stale=now - group.observed_at > limit,
                source=group.source,
            )
            for metric, group in newest.items()
        },
        last_sample=None
        if last_sample is None
        else LastSampleView(at=last_sample.observed_at, agent=last_sample.agent),
        last_probe=None
        if last_probe is None
        else LastProbeView(at=last_probe.started_at, ok=last_probe.ok, error=last_probe.error),
    )


class ProbeTrigger:
    """Asks the prober to look, when the newest reading is overdue, at most once per
    `min_gap_seconds` per process. The prober decides again whether a probe is due."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        after_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        min_gap_seconds: float = 60,
    ) -> None:
        self.pool = pool
        self.after = timedelta(seconds=after_seconds)
        self.clock = clock
        self.min_gap = min_gap_seconds
        self.last: float | None = None

    async def maybe(self, newest_at: datetime | None, now: datetime) -> bool:
        """True when a NOTIFY was sent. A failure is logged and never raised."""
        if newest_at is not None and now - newest_at <= self.after:
            return False
        at = self.clock()
        if self.last is not None and at - self.last < self.min_gap:
            return False
        self.last = at  # before the await: concurrent requests send one NOTIFY
        try:
            async with self.pool.connection() as conn:
                await notify(conn, PROBE_CHANNEL)
        except Exception as exc:
            log.warning("probe request not sent: %s: %s", type(exc).__name__, exc)
            return False
        return True


def _spawn(app: FastAPI, work: Awaitable[object]) -> None:
    """Run `work` in the background; the app keeps the task so it is not collected early."""
    task = asyncio.ensure_future(work)
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)


@router.get("/current", response_model=CurrentView)
async def current(request: Request) -> CurrentView:
    app = request.app
    now = datetime.now(UTC)
    trigger = app.state.probe_trigger
    async with app.state.pool.connection() as conn:
        groups = await readings.window_groups(conn, now)
        newest_at = None if trigger is None else await readings.newest_at(conn)
        last_sample = await samples.latest(conn)
        last_probe = await probes.latest(conn)
    view = current_view(groups, last_sample, last_probe, now, app.state.settings.stale_seconds)
    if trigger is not None:
        _spawn(app, trigger.maybe(newest_at, now))
    return view


@router.get("/history", response_model=HistoryView, response_model_by_alias=True)
async def history(
    request: Request,
    frm: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    metric: Annotated[str | None, Query(pattern=METRIC_PATTERN)] = None,
    max_points: Annotated[int, Query(ge=10, le=1000)] = 500,
) -> HistoryView:
    try:
        start, end = resolve_range(frm, to, datetime.now(UTC))
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    width = bucket_seconds(start, end, max_points)
    async with request.app.state.pool.connection() as conn:
        rows = await readings.history(conn, metric, start, end, width)
    return HistoryView(
        from_=start,
        to=end,
        bucket_seconds=width,
        points=[
            PointView(
                observed_at=r.observed_at,
                metric=r.metric,
                percent=r.percent,
                resets_at=r.resets_at,
                source=r.source,
            )
            for r in rows
        ],
    )
