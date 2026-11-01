"""Parsers for the usage windows: the probe's stream-json and the statusLine rate_limits.

Percent is on the 0-100 scale everywhere. The probe's `utilization` (0-1) is multiplied by 100
here, once; the statusLine `used_percentage` (documented 0-100) is taken as is.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

MAX_PERCENT = 1000.0

_METRIC = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class Window:
    metric: str
    percent: float
    resets_at: datetime


@dataclass(frozen=True)
class StreamResult:
    event: dict | None
    windows: dict[str, Window]
    cli_version: str | None
    is_error: bool
    result_text: str | None
    cost_usd: float | None


def _finite(value: object) -> float | None:
    """A finite number (bool is not one) as a float, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _window(metric: object, percent: object, resets_at: object, scale: float) -> Window | None:
    """One validated window, or None when any part is unusable."""
    if not isinstance(metric, str) or not _METRIC.match(metric):
        return None
    raw_percent = _finite(percent)
    raw_reset = _finite(resets_at)
    if raw_percent is None or raw_reset is None:
        return None
    scaled = raw_percent * scale
    if not 0 <= scaled <= MAX_PERCENT:
        return None
    try:
        reset = datetime.fromtimestamp(raw_reset, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return Window(metric, round(scaled, 2), reset)


def windows_from_event(rate_limit_info: dict) -> dict[str, Window]:
    """Windows of a probe `rate_limit_info`: `unifiedWindows` (observed on 2.1.304, undocumented)
    when present, else the top-level `rateLimitType`/`utilization`/`resetsAt`."""
    if not isinstance(rate_limit_info, dict):
        return {}
    unified = rate_limit_info.get("unifiedWindows")
    if isinstance(unified, dict):
        entries = [
            (metric, entry.get("utilization"), entry.get("resetsAt"))
            for metric, entry in unified.items()
            if isinstance(entry, dict)
        ]
    else:
        entries = [
            (
                rate_limit_info.get("rateLimitType"),
                rate_limit_info.get("utilization"),
                rate_limit_info.get("resetsAt"),
            )
        ]
    built = (
        _window(metric, utilization, resets_at, 100.0) for metric, utilization, resets_at in entries
    )
    return {w.metric: w for w in built if w is not None}


def windows_from_statusline(rate_limits: object) -> dict[str, Window]:
    """Windows of the statusLine `rate_limits` (`used_percentage` 0-100, `resets_at` epoch)."""
    if not isinstance(rate_limits, dict):
        return {}
    built = (
        _window(metric, entry.get("used_percentage"), entry.get("resets_at"), 1.0)
        for metric, entry in rate_limits.items()
        if isinstance(entry, dict)
    )
    return {w.metric: w for w in built if w is not None}


def parse_stream(text: str) -> StreamResult:
    """Parse a `claude -p` stream-json run; lines that are not JSON objects are skipped.

    The last `rate_limit_event` and the last `result` line win.
    """
    event: dict | None = None
    windows: dict[str, Window] = {}
    cli_version: str | None = None
    is_error = False
    result_text: str | None = None
    cost_usd: float | None = None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "rate_limit_event":
            event = obj
            windows = windows_from_event(obj.get("rate_limit_info"))  # type: ignore[arg-type]
        elif kind == "system" and obj.get("subtype") == "init":
            version = obj.get("claude_code_version")
            cli_version = version if isinstance(version, str) else cli_version
        elif kind == "result":
            is_error = obj.get("is_error") is True
            result = obj.get("result")
            result_text = result if isinstance(result, str) else None
            cost_usd = _finite(obj.get("total_cost_usd"))
    return StreamResult(event, windows, cli_version, is_error, result_text, cost_usd)
