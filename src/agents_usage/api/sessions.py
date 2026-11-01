"""GET /usage/v1/sessions: the session start and end marks the web UI draws under the chart."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from agents_usage.api.errors import ApiError
from agents_usage.api.ranges import resolve_range
from agents_usage.api.schemas import MarkView, SessionsView
from agents_usage.db import marks

router = APIRouter()


@router.get("/sessions", response_model=SessionsView, response_model_by_alias=True)
async def sessions(
    request: Request,
    frm: Annotated[
        datetime | None,
        Query(alias="from", description="Start of the range (default: to minus 7 days)"),
    ] = None,
    to: Annotated[datetime | None, Query(description="End of the range (default: now)")] = None,
    limit: Annotated[int, Query(ge=1, le=2000, description="Marks at most")] = 1000,
) -> SessionsView:
    try:
        start, end = resolve_range(frm, to, datetime.now(UTC))
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    async with request.app.state.pool.connection() as conn:
        rows = await marks.overlapping(conn, start, end, limit)
    return SessionsView(
        from_=start,
        to=end,
        truncated=len(rows) > limit,
        marks=[
            MarkView(
                session_id=r.session_id,
                slug=r.slug,
                preset_id=r.preset_id,
                preset_name=r.preset_name,
                origin=r.origin,
                started_at=r.started_at,
                ended_at=r.ended_at,
            )
            for r in rows[:limit]
        ],
    )
