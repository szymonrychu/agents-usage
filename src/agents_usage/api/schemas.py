"""Request bodies of the ingest listener."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

AGENT_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class SampleIn(BaseModel):
    """What the wrapper of an agent pod posts: its statusLine `rate_limits` as received."""

    model_config = ConfigDict(extra="forbid")

    agent: str | None = Field(default=None, pattern=AGENT_PATTERN)
    cli_version: str | None = Field(default=None, max_length=64)
    rate_limits: dict[str, Any]
