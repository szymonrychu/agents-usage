"""Request bodies of the ingest listener and the views of the read routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

AGENT_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class SampleIn(BaseModel):
    """What the wrapper of an agent pod posts: its statusLine `rate_limits` as received."""

    model_config = ConfigDict(extra="forbid")

    agent: str | None = Field(default=None, pattern=AGENT_PATTERN)
    cli_version: str | None = Field(default=None, max_length=64)
    rate_limits: dict[str, Any]


Source = Literal["statusline", "probe"]
# Every instant is served in UTC, whatever time zone the database session uses.
UtcDatetime = Annotated[datetime, AfterValidator(lambda value: value.astimezone(UTC))]


class WindowView(BaseModel):
    """The current reading of one metric: the highest percent of its newest window."""

    percent: float
    resets_at: UtcDatetime
    observed_at: UtcDatetime
    stale: bool
    source: Source


class LastSampleView(BaseModel):
    at: UtcDatetime
    agent: str | None


class LastProbeView(BaseModel):
    at: UtcDatetime
    ok: bool
    error: str | None


class CurrentView(BaseModel):
    windows: dict[str, WindowView]
    last_sample: LastSampleView | None
    last_probe: LastProbeView | None


class PointView(BaseModel):
    observed_at: UtcDatetime
    metric: str
    percent: float
    resets_at: UtcDatetime
    source: Source


class HistoryView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    bucket_seconds: int
    points: list[PointView]
