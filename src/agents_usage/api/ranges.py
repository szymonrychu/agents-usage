"""The `from`/`to` range shared by the read routes."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

DEFAULT_SPAN = timedelta(days=7)
MAX_SPAN = timedelta(days=100)


def _aware(value: datetime) -> datetime:
    """A naive datetime is taken as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def resolve_range(
    frm: datetime | None, to: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    """The range to read: `to` defaults to `now`, `from` to seven days before `to`. Raises
    ValueError when `from` is not before `to` or the range is longer than 100 days."""
    end = now if to is None else _aware(to)
    start = end - DEFAULT_SPAN if frm is None else _aware(frm)
    if start >= end:
        raise ValueError("from must be before to")
    if end - start > MAX_SPAN:
        raise ValueError("the range must not exceed 100 days")
    return start, end


def bucket_seconds(frm: datetime, to: datetime, max_points: int) -> int:
    """The bucket width that keeps a range within `max_points` buckets."""
    return max(1, math.ceil((to - frm).total_seconds() / max_points))
