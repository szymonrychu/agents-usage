from datetime import UTC, datetime, timedelta

import pytest

from agents_usage.api.ranges import bucket_seconds, resolve_range

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_default_range_is_last_7_days():
    assert resolve_range(None, None, NOW) == (NOW - timedelta(days=7), NOW)


def test_default_from_follows_a_given_to():
    to = NOW - timedelta(days=1)
    assert resolve_range(None, to, NOW) == (to - timedelta(days=7), to)


def test_from_must_precede_to():
    with pytest.raises(ValueError):
        resolve_range(NOW, NOW, NOW)
    with pytest.raises(ValueError):
        resolve_range(NOW + timedelta(seconds=1), NOW, NOW)


def test_range_over_100_days_refused():
    with pytest.raises(ValueError):
        resolve_range(NOW - timedelta(days=101), NOW, NOW)
    assert resolve_range(NOW - timedelta(days=100), NOW, NOW) == (NOW - timedelta(days=100), NOW)


def test_naive_datetimes_are_utc():
    naive = datetime(2026, 10, 30, 0, 0)
    frm, to = resolve_range(naive, None, NOW)
    assert frm == naive.replace(tzinfo=UTC)
    assert to == NOW


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(hours=24), 173),
        (timedelta(days=7), 1210),
        (timedelta(days=30), 5184),
        (timedelta(days=90), 15552),
        (timedelta(seconds=1), 1),
    ],
)
def test_bucket_seconds(span, expected):
    assert bucket_seconds(NOW - span, NOW, 500) == expected
