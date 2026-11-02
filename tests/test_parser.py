import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents_usage.parser import (
    MAX_PERCENT,
    MAX_RESET_AHEAD,
    RESET_PAST_SKEW,
    StreamResult,
    Window,
    parse_stream,
    windows_from_event,
    windows_from_statusline,
)

RESET = datetime.fromtimestamp(1794049200, UTC)
RESET_FIVE = datetime.fromtimestamp(1794020400, UTC)
OBSERVED = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def event_line(info: dict) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def unified(**windows: object) -> dict:
    return {"status": "allowed", "unifiedWindows": windows}


def test_probe_seven_day():
    r = parse_stream(fixture("haiku-2.1.304.jsonl"), OBSERVED)
    assert r.windows == {"seven_day": Window("seven_day", 22.0, RESET)}
    assert r.event["rate_limit_info"]["rateLimitType"] == "seven_day"
    assert r.event["rate_limit_info"]["status"] == "allowed"
    assert (r.cli_version, r.is_error, r.cost_usd) == ("2.1.304", False, 0.0043312)
    assert r.result_text == "ok"


def test_usage_command_has_no_event():
    r = parse_stream(fixture("usage-command-2.1.304.jsonl"), OBSERVED)
    assert r.event is None
    assert r.windows == {}
    assert r.cli_version == "2.1.304"
    assert r.is_error is False


def test_statusline_windows():
    w = windows_from_statusline(json.loads(fixture("statusline-rate-limits.json")), OBSERVED)
    assert w["seven_day"] == Window("seven_day", 22.0, RESET)
    assert w["five_hour"] == Window("five_hour", 23.5, RESET_FIVE)


def test_units_guard_same_state_both_shapes():
    probe = windows_from_event(
        unified(seven_day={"utilization": 0.22, "resetsAt": 1794049200}), OBSERVED
    )
    status = windows_from_statusline(
        {"seven_day": {"used_percentage": 22, "resets_at": 1794049200}}, OBSERVED
    )
    assert probe == status == {"seven_day": Window("seven_day", 22.0, RESET)}


def test_statusline_fraction_not_rescaled():
    w = windows_from_statusline(
        {"five_hour": {"used_percentage": 0.22, "resets_at": 1794020400}}, OBSERVED
    )
    assert w["five_hour"].percent == 0.22


@pytest.mark.parametrize(
    "bad",
    [
        {"used_usd": 3},
        {"used_percentage": "a", "resets_at": 1794020400},
        {"used_percentage": True, "resets_at": 1794020400},
        {"used_percentage": math.nan, "resets_at": 1794020400},
        {"used_percentage": math.inf, "resets_at": 1794020400},
        {"used_percentage": -1, "resets_at": 1794020400},
        {"used_percentage": 1000.1, "resets_at": 1794020400},
        {"used_percentage": 5},
        {"used_percentage": 5, "resets_at": "x"},
        {"used_percentage": 5, "resets_at": True},
        {"used_percentage": 5, "resets_at": math.nan},
        {"used_percentage": 5, "resets_at": 1e20},
        "not a dict",
        None,
    ],
)
def test_statusline_skips_bad_entries(bad):
    good = {"used_percentage": 7, "resets_at": 1794049200}
    w = windows_from_statusline({"bad_one": bad, "Bad-Key": good, "seven_day": good}, OBSERVED)
    assert w == {"seven_day": Window("seven_day", 7.0, RESET)}


@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_statusline_not_a_dict(value):
    assert windows_from_statusline(value, OBSERVED) == {}


def test_last_event_wins():
    text = "\n".join(
        [
            event_line(unified(seven_day={"utilization": 0.1, "resetsAt": 1794049200})),
            event_line(unified(seven_day={"utilization": 0.3, "resetsAt": 1794049200})),
        ]
    )
    r = parse_stream(text, OBSERVED)
    assert r.windows["seven_day"].percent == 30.0
    assert r.event["rate_limit_info"]["unifiedWindows"]["seven_day"]["utilization"] == 0.3


def test_all_unified_keys_kept():
    w = windows_from_event(
        unified(
            five_hour={"utilization": 0.5, "resetsAt": 1794020400},
            seven_day={"utilization": 0.22, "resetsAt": 1794049200},
            seven_day_opus={"utilization": 0.01, "resetsAt": 1794049200},
        ),
        OBSERVED,
    )
    assert set(w) == {"five_hour", "seven_day", "seven_day_opus"}
    assert w["five_hour"] == Window("five_hour", 50.0, RESET_FIVE)


def test_top_level_fallback():
    w = windows_from_event(
        {
            "status": "allowed",
            "rateLimitType": "five_hour",
            "utilization": 0.5,
            "resetsAt": 1794020400,
        },
        OBSERVED,
    )
    assert w == {"five_hour": Window("five_hour", 50.0, RESET_FIVE)}


def test_top_level_without_utilization_is_no_data():
    info = {"status": "allowed", "resetsAt": 1794020400, "rateLimitType": "five_hour"}
    assert windows_from_event(info, OBSERVED) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"resetsAt": 1794020400},
        {"utilization": -0.1, "resetsAt": 1794020400},
        {"utilization": "0.5", "resetsAt": 1794020400},
        {"utilization": True, "resetsAt": 1794020400},
        {"utilization": math.nan, "resetsAt": 1794020400},
        {"utilization": math.inf, "resetsAt": 1794020400},
        {"utilization": 10.001, "resetsAt": 1794020400},
        {"utilization": 0.5},
        {"utilization": 0.5, "resetsAt": "1794020400"},
        {"utilization": 0.5, "resetsAt": math.nan},
        {"utilization": 0.5, "resetsAt": math.inf},
        {"utilization": 0.5, "resetsAt": 1e20},
        {"utilization": 0.5, "resetsAt": -1e20},
        "not a dict",
    ],
)
def test_bad_window_entries_skipped(bad):
    good = {"utilization": 0.25, "resetsAt": 1794049200}
    w = windows_from_event(unified(bad_one=bad, **{"Bad-Key": good}, seven_day=good), OBSERVED)
    assert w == {"seven_day": Window("seven_day", 25.0, RESET)}


def test_bad_metric_names_skipped():
    good = {"utilization": 0.25, "resetsAt": 1794049200}
    names = ["Bad-Key", "", "x" * 65, "a b", "UPPER", "seven_day\n"]
    w = windows_from_event(
        unified(**{n: good for n in names}, ok_1=good, **{"y" * 64: good}), OBSERVED
    )
    assert set(w) == {"ok_1", "y" * 64}
    top = {"rateLimitType": "Bad-Key", "utilization": 0.5, "resetsAt": 1794049200}
    assert windows_from_event(top, OBSERVED) == {}
    status = {"seven_day\n": {"used_percentage": 7, "resets_at": 1794049200}}
    assert windows_from_statusline(status, OBSERVED) == {}


def test_windows_from_event_not_a_dict():
    assert windows_from_event(None, OBSERVED) == {}  # type: ignore[arg-type]
    assert windows_from_event([], OBSERVED) == {}  # type: ignore[arg-type]


def test_over_100_kept():
    probe = windows_from_event(
        unified(seven_day={"utilization": 1.03, "resetsAt": 1794049200}), OBSERVED
    )
    assert probe["seven_day"].percent == 103.0
    status = windows_from_statusline(
        {"seven_day": {"used_percentage": 103, "resets_at": 1794049200}}, OBSERVED
    )
    assert status["seven_day"].percent == 103.0
    assert MAX_PERCENT == 1000.0


def test_percent_rounded_to_two_places():
    w = windows_from_event(
        unified(seven_day={"utilization": 0.123456, "resetsAt": 1794049200}), OBSERVED
    )
    assert w["seven_day"].percent == 12.35
    s = windows_from_statusline(
        {"seven_day": {"used_percentage": 12.3456, "resets_at": 1794049200}}, OBSERVED
    )
    assert s["seven_day"].percent == 12.35


def test_garbage_lines_skipped():
    lines = fixture("haiku-2.1.304.jsonl").splitlines()
    noisy = ["not json", "[1]", "", "42", '"x"', "null", lines[0], "{broken", *lines[1:], "  "]
    assert parse_stream("\n".join(noisy), OBSERVED) == parse_stream("\n".join(lines), OBSERVED)


def test_error_result():
    text = json.dumps(
        {"type": "result", "is_error": True, "result": "Not logged in - Please run /login"}
    )
    r = parse_stream(text, OBSERVED)
    assert r.is_error is True
    assert r.result_text == "Not logged in - Please run /login"
    assert r.event is None
    assert r.windows == {}
    assert r.cost_usd is None


def test_empty_stream():
    assert parse_stream("", OBSERVED) == StreamResult(
        event=None, windows={}, cli_version=None, is_error=False, result_text=None, cost_usd=None
    )


def test_event_with_bad_info_keeps_event_without_windows():
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": "x"})
    r = parse_stream(line, OBSERVED)
    assert r.event == {"type": "rate_limit_event", "rate_limit_info": "x"}
    assert r.windows == {}


def _epoch(offset: timedelta) -> int:
    return int((OBSERVED + offset).timestamp())


def _status(**resets: int) -> dict:
    return {m: {"used_percentage": 10, "resets_at": at} for m, at in resets.items()}


def _unified(**resets: int) -> dict:
    return unified(**{m: {"utilization": 0.1, "resetsAt": at} for m, at in resets.items()})


def test_bound_values():
    assert timedelta(days=8) == MAX_RESET_AHEAD
    assert timedelta(seconds=60) == RESET_PAST_SKEW


@pytest.mark.parametrize("parse", [windows_from_statusline, windows_from_event])
@pytest.mark.parametrize(
    ("offset", "kept"),
    [
        (timedelta(days=8, seconds=1), False),
        (timedelta(days=8), True),
        (timedelta(days=8) - timedelta(seconds=1), True),
        (timedelta(days=3650), False),
        (timedelta(hours=5), True),
        (timedelta(0), True),
        (-timedelta(seconds=59), True),
        (-timedelta(seconds=60), True),
        (-timedelta(seconds=61), False),
        (-timedelta(days=1), False),
        (-timedelta(days=3650), False),
    ],
)
def test_reset_bound(parse, offset, kept):
    at = _epoch(offset)
    raw = _status(seven_day=at) if parse is windows_from_statusline else _unified(seven_day=at)
    got = parse(raw, OBSERVED)
    assert (set(got) == {"seven_day"}) is kept
    if kept:
        assert got["seven_day"].resets_at == datetime.fromtimestamp(at, UTC)


def test_mixed_statusline_sample_keeps_the_plausible_windows(caplog):
    far, past = _epoch(timedelta(days=400)), _epoch(-timedelta(hours=2))
    good = _epoch(timedelta(hours=3))
    with caplog.at_level(logging.WARNING, logger="agents_usage.parser"):
        got = windows_from_statusline(
            _status(five_hour=good, seven_day=far, seven_day_opus=past), OBSERVED
        )
    assert set(got) == {"five_hour"}
    (record,) = caplog.records
    assert "2 window" in record.getMessage()
    assert "seven_day" in record.getMessage() and "seven_day_opus" in record.getMessage()


def test_mixed_probe_sample_keeps_the_plausible_windows(caplog):
    far, good = _epoch(timedelta(days=400)), _epoch(timedelta(days=2))
    with caplog.at_level(logging.WARNING, logger="agents_usage.parser"):
        got = windows_from_event(_unified(five_hour=far, seven_day=good), OBSERVED)
    assert set(got) == {"seven_day"}
    assert len(caplog.records) == 1


def test_plausible_sample_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="agents_usage.parser"):
        windows_from_statusline(_status(five_hour=_epoch(timedelta(hours=3))), OBSERVED)
    assert caplog.records == []


def test_probe_stream_goes_through_the_bound():
    line = event_line(_unified(seven_day=_epoch(timedelta(days=30))))
    r = parse_stream(line, OBSERVED)
    assert r.event is not None  # the event is kept verbatim, only the window is dropped
    assert r.windows == {}
    assert parse_stream(line, OBSERVED + timedelta(days=25)).windows != {}


def test_bound_is_relative_to_the_observed_time():
    at = _epoch(timedelta(days=10))
    assert windows_from_statusline(_status(seven_day=at), OBSERVED) == {}
    assert set(windows_from_statusline(_status(seven_day=at), OBSERVED + timedelta(days=3))) == {
        "seven_day"
    }
