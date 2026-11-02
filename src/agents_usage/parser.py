"""Parsers for the usage windows: the probe's stream-json and the statusLine rate_limits.

Percent is on the 0-100 scale everywhere. The probe's `utilization` (0-1) is multiplied by 100
here, once; the statusLine `used_percentage` (documented 0-100) is taken as is.

A window whose `resets_at` is implausible against the time the sample was observed (more than
`MAX_RESET_AHEAD` ahead, or more than `RESET_PAST_SKEW` behind) is dropped and logged; the rest of
the sample is kept. Both sources go through the same bound.
"""

import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

MAX_PERCENT = 1000.0
# The longest window is the 7-day one; a day more than the 7 days (the same 8 days as the readings
# lookback) leaves room for a clock that runs ahead. Later than that is a bad or forged sample.
MAX_RESET_AHEAD = timedelta(days=8)
# A reset in the past is a window that already ended. A minute of skew is allowed: that is the
# precision a window is grouped by (`date_trunc('minute', resets_at)`), so a sample posted as the
# window rolled over is not counted as bad.
RESET_PAST_SKEW = timedelta(seconds=60)

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
    if not isinstance(metric, str) or not _METRIC.fullmatch(metric):
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


def _plausible(reset: datetime, observed_at: datetime) -> bool:
    return observed_at - RESET_PAST_SKEW <= reset <= observed_at + MAX_RESET_AHEAD


def _windows(
    entries: Iterable[tuple[object, object, object]], scale: float, observed_at: datetime
) -> dict[str, Window]:
    """The valid windows of `(metric, percent, resets_at)` entries whose reset is plausible
    against `observed_at`; the dropped ones are logged once, with their count."""
    built = (_window(metric, percent, resets_at, scale) for metric, percent, resets_at in entries)
    kept: dict[str, Window] = {}
    dropped: list[Window] = []
    for window in built:
        if window is None:
            continue
        if _plausible(window.resets_at, observed_at):
            kept[window.metric] = window
        else:
            dropped.append(window)
    if dropped:
        log.warning(
            "dropped %d window(s) with an implausible resets_at (observed %s): %s",
            len(dropped),
            observed_at.isoformat(),
            ", ".join(f"{w.metric}={w.resets_at.isoformat()}" for w in dropped),
        )
    return kept


def windows_from_event(rate_limit_info: dict, observed_at: datetime) -> dict[str, Window]:
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
    return _windows(entries, 100.0, observed_at)


def windows_from_statusline(rate_limits: object, observed_at: datetime) -> dict[str, Window]:
    """Windows of the statusLine `rate_limits` (`used_percentage` 0-100, `resets_at` epoch)."""
    if not isinstance(rate_limits, dict):
        return {}
    entries = (
        (metric, entry.get("used_percentage"), entry.get("resets_at"))
        for metric, entry in rate_limits.items()
        if isinstance(entry, dict)
    )
    return _windows(entries, 1.0, observed_at)


def parse_stream(text: str, observed_at: datetime) -> StreamResult:
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
            windows = windows_from_event(obj.get("rate_limit_info"), observed_at)  # type: ignore[arg-type]
        elif kind == "system" and obj.get("subtype") == "init":
            version = obj.get("claude_code_version")
            cli_version = version if isinstance(version, str) else cli_version
        elif kind == "result":
            is_error = obj.get("is_error") is True
            result = obj.get("result")
            result_text = result if isinstance(result, str) else None
            cost_usd = _finite(obj.get("total_cost_usd"))
    return StreamResult(event, windows, cli_version, is_error, result_text, cost_usd)
