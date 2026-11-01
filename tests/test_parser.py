import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents_usage.parser import (
    MAX_PERCENT,
    StreamResult,
    Window,
    parse_stream,
    windows_from_event,
    windows_from_statusline,
)

RESET = datetime.fromtimestamp(1794049200, UTC)
RESET_FIVE = datetime.fromtimestamp(1794020400, UTC)
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def event_line(info: dict) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def unified(**windows: object) -> dict:
    return {"status": "allowed", "unifiedWindows": windows}


def test_probe_seven_day():
    r = parse_stream(fixture("haiku-2.1.304.jsonl"))
    assert r.windows == {"seven_day": Window("seven_day", 22.0, RESET)}
    assert r.event["rate_limit_info"]["rateLimitType"] == "seven_day"
    assert r.event["rate_limit_info"]["status"] == "allowed"
    assert (r.cli_version, r.is_error, r.cost_usd) == ("2.1.304", False, 0.0043312)
    assert r.result_text == "ok"


def test_usage_command_has_no_event():
    r = parse_stream(fixture("usage-command-2.1.304.jsonl"))
    assert r.event is None
    assert r.windows == {}
    assert r.cli_version == "2.1.304"
    assert r.is_error is False


def test_statusline_windows():
    w = windows_from_statusline(json.loads(fixture("statusline-rate-limits.json")))
    assert w["seven_day"] == Window("seven_day", 22.0, RESET)
    assert w["five_hour"] == Window("five_hour", 23.5, RESET_FIVE)


def test_units_guard_same_state_both_shapes():
    probe = windows_from_event(unified(seven_day={"utilization": 0.22, "resetsAt": 1794049200}))
    status = windows_from_statusline(
        {"seven_day": {"used_percentage": 22, "resets_at": 1794049200}}
    )
    assert probe == status == {"seven_day": Window("seven_day", 22.0, RESET)}


def test_statusline_fraction_not_rescaled():
    w = windows_from_statusline({"five_hour": {"used_percentage": 0.22, "resets_at": 1794020400}})
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
    w = windows_from_statusline({"bad_one": bad, "Bad-Key": good, "seven_day": good})
    assert w == {"seven_day": Window("seven_day", 7.0, RESET)}


@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_statusline_not_a_dict(value):
    assert windows_from_statusline(value) == {}


def test_last_event_wins():
    text = "\n".join(
        [
            event_line(unified(seven_day={"utilization": 0.1, "resetsAt": 1794049200})),
            event_line(unified(seven_day={"utilization": 0.3, "resetsAt": 1794049200})),
        ]
    )
    r = parse_stream(text)
    assert r.windows["seven_day"].percent == 30.0
    assert r.event["rate_limit_info"]["unifiedWindows"]["seven_day"]["utilization"] == 0.3


def test_all_unified_keys_kept():
    w = windows_from_event(
        unified(
            five_hour={"utilization": 0.5, "resetsAt": 1794020400},
            seven_day={"utilization": 0.22, "resetsAt": 1794049200},
            seven_day_opus={"utilization": 0.01, "resetsAt": 1794049200},
        )
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
        }
    )
    assert w == {"five_hour": Window("five_hour", 50.0, RESET_FIVE)}


def test_top_level_without_utilization_is_no_data():
    info = {"status": "allowed", "resetsAt": 1794020400, "rateLimitType": "five_hour"}
    assert windows_from_event(info) == {}


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
    w = windows_from_event(unified(bad_one=bad, **{"Bad-Key": good}, seven_day=good))
    assert w == {"seven_day": Window("seven_day", 25.0, RESET)}


def test_bad_metric_names_skipped():
    good = {"utilization": 0.25, "resetsAt": 1794049200}
    names = ["Bad-Key", "", "x" * 65, "a b", "UPPER", "seven_day\n"]
    w = windows_from_event(unified(**{n: good for n in names}, ok_1=good, **{"y" * 64: good}))
    assert set(w) == {"ok_1", "y" * 64}
    top = {"rateLimitType": "Bad-Key", "utilization": 0.5, "resetsAt": 1794049200}
    assert windows_from_event(top) == {}
    status = {"seven_day\n": {"used_percentage": 7, "resets_at": 1794049200}}
    assert windows_from_statusline(status) == {}


def test_windows_from_event_not_a_dict():
    assert windows_from_event(None) == {}  # type: ignore[arg-type]
    assert windows_from_event([]) == {}  # type: ignore[arg-type]


def test_over_100_kept():
    probe = windows_from_event(unified(seven_day={"utilization": 1.03, "resetsAt": 1794049200}))
    assert probe["seven_day"].percent == 103.0
    status = windows_from_statusline(
        {"seven_day": {"used_percentage": 103, "resets_at": 1794049200}}
    )
    assert status["seven_day"].percent == 103.0
    assert MAX_PERCENT == 1000.0


def test_percent_rounded_to_two_places():
    w = windows_from_event(unified(seven_day={"utilization": 0.123456, "resetsAt": 1794049200}))
    assert w["seven_day"].percent == 12.35
    s = windows_from_statusline(
        {"seven_day": {"used_percentage": 12.3456, "resets_at": 1794049200}}
    )
    assert s["seven_day"].percent == 12.35


def test_garbage_lines_skipped():
    lines = fixture("haiku-2.1.304.jsonl").splitlines()
    noisy = ["not json", "[1]", "", "42", '"x"', "null", lines[0], "{broken", *lines[1:], "  "]
    assert parse_stream("\n".join(noisy)) == parse_stream("\n".join(lines))


def test_error_result():
    text = json.dumps(
        {"type": "result", "is_error": True, "result": "Not logged in - Please run /login"}
    )
    r = parse_stream(text)
    assert r.is_error is True
    assert r.result_text == "Not logged in - Please run /login"
    assert r.event is None
    assert r.windows == {}
    assert r.cost_usd is None


def test_empty_stream():
    assert parse_stream("") == StreamResult(
        event=None, windows={}, cli_version=None, is_error=False, result_text=None, cost_usd=None
    )


def test_event_with_bad_info_keeps_event_without_windows():
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": "x"})
    r = parse_stream(line)
    assert r.event == {"type": "rate_limit_event", "rate_limit_info": "x"}
    assert r.windows == {}
