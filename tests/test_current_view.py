from datetime import UTC, datetime, timedelta, timezone

from agents_usage.api.schemas import CurrentView, LastProbeView, LastSampleView, WindowView
from agents_usage.api.usage import current_view
from agents_usage.db.probes import ProbeRow
from agents_usage.db.readings import WindowGroup
from agents_usage.db.samples import SampleRow

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
STALE = 1800


def group(metric="seven_day", percent=22.0, resets=None, observed=None, source="statusline"):
    return WindowGroup(
        metric=metric,
        resets_at=resets or NOW + timedelta(days=1),
        percent=percent,
        observed_at=observed or NOW - timedelta(seconds=60),
        source=source,
    )


def view(groups, sample=None, probe=None):
    return current_view(groups, sample, probe, NOW, STALE)


def test_fresh_window():
    g = group()
    assert view([g]).windows["seven_day"] == WindowView(
        percent=22.0,
        resets_at=g.resets_at,
        observed_at=g.observed_at,
        stale=False,
        source="statusline",
    )


def test_latest_reset_group_wins():
    old = group(percent=90.0, resets=NOW + timedelta(hours=1))
    new = group(percent=3.0, resets=NOW + timedelta(days=7))
    assert view([old, new]).windows["seven_day"].percent == 3.0
    assert view([new, old]).windows["seven_day"].percent == 3.0


def test_metrics_are_independent():
    five = group(metric="five_hour", percent=40.0, resets=NOW + timedelta(hours=2))
    seven = group()
    assert set(view([five, seven]).windows) == {"five_hour", "seven_day"}


def test_reset_window_dropped():
    assert view([group(resets=NOW - timedelta(seconds=1))]).windows == {}


def test_reset_exactly_now_dropped():
    assert view([group(resets=NOW)]).windows == {}


def test_stale_after_1800s():
    stale = group(observed=NOW - timedelta(seconds=1801))
    edge = group(observed=NOW - timedelta(seconds=1800))
    assert view([stale]).windows["seven_day"].stale is True
    assert view([edge]).windows["seven_day"].stale is False


def test_probe_source_is_kept():
    assert view([group(source="probe")]).windows["seven_day"].source == "probe"


def test_last_sample_and_probe():
    sample = SampleRow(observed_at=NOW - timedelta(seconds=5), agent="agent-x-0")
    probe = ProbeRow(started_at=NOW - timedelta(hours=1), ok=False, error="timeout")
    result = view([], sample, probe)
    assert result.last_sample == LastSampleView(at=sample.observed_at, agent="agent-x-0")
    assert result.last_probe == LastProbeView(at=probe.started_at, ok=False, error="timeout")


def test_nothing_yet():
    assert view([]) == CurrentView(windows={}, last_sample=None, last_probe=None)


def test_instants_are_served_in_utc():
    offset = timezone(timedelta(hours=2))
    g = group(resets=(NOW + timedelta(days=1)).astimezone(offset))
    body = view([g]).model_dump(mode="json")
    assert body["windows"]["seven_day"]["resets_at"].endswith("Z")
    assert body["windows"]["seven_day"]["resets_at"] == "2026-11-02T12:00:00Z"
