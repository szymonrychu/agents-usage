"""Classifying a claude run into a ProbeRecord, and redaction (no database)."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agents_usage.parser import Window
from agents_usage.prober import redact, to_record
from agents_usage.runner import CliRun, run_cli

FIXTURES = Path(__file__).parent / "fixtures"
HAIKU = (FIXTURES / "haiku-2.1.304.jsonl").read_text()
USAGE_COMMAND = (FIXTURES / "usage-command-2.1.304.jsonl").read_text()
FAKE = str(Path(__file__).parent / "fake_claude.py")
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 1, 12, 0, 2, tzinfo=UTC)
TOKEN = "fake-oauth-token"


def event_line() -> dict:
    return next(
        json.loads(line)
        for line in HAIKU.splitlines()
        if json.loads(line).get("type") == "rate_limit_event"
    )


def test_ok_record():
    rec = to_record(T0, T1, CliRun(0, HAIKU, "", 1934, False), TOKEN)
    assert rec.ok is True
    assert rec.error is None and rec.detail is None
    assert rec.windows == {
        "seven_day": Window("seven_day", 22.0, datetime.fromtimestamp(1794049200, UTC))
    }
    assert rec.cli_version == "2.1.304"
    assert rec.cost_usd == 0.0043312
    assert rec.raw == event_line()
    assert rec.raw["rate_limit_info"]["rateLimitType"] == "seven_day"
    assert (rec.started_at, rec.observed_at, rec.duration_ms) == (T0, T1, 1934)


def test_timeout_record():
    rec = to_record(T0, T1, CliRun(None, HAIKU, "", 120000, True), TOKEN)
    assert rec.ok is False
    assert rec.error == "timeout"
    assert rec.windows == {}


def test_cli_error_record():
    stdout = json.dumps({"type": "result", "is_error": True, "result": "Not logged in"})
    rec = to_record(T0, T1, CliRun(1, stdout, "", 5, False), TOKEN)
    assert (rec.ok, rec.error, rec.detail) == (False, "cli_error", "Not logged in")
    assert rec.windows == {}


def test_cli_exit_record():
    rec = to_record(T0, T1, CliRun(2, "", "boom", 5, False), TOKEN)
    assert (rec.ok, rec.error, rec.detail) == (False, "cli_exit", "boom")
    assert rec.raw is None


def test_no_event_record():
    rec = to_record(T0, T1, CliRun(0, USAGE_COMMAND, "", 5, False), TOKEN)
    assert (rec.ok, rec.error) == (False, "no_rate_limit_event")
    assert rec.windows == {}
    assert rec.cli_version == "2.1.304"


def test_event_wins_over_exit_code():
    rec = to_record(T0, T1, CliRun(1, HAIKU, "warning", 5, False), TOKEN)
    assert rec.ok is True and rec.error is None
    assert set(rec.windows) == {"seven_day"}


def test_token_never_stored():
    run = CliRun(1, "", "auth failed for tok-123 tok-123", 5, False)
    rec = to_record(T0, T1, run, "tok-123")
    assert "tok-123" not in (rec.detail or "") and "[redacted]" in rec.detail


def test_token_redacted_from_result_text_and_raw():
    token = "fake-oauth-token-xyz"
    event = event_line()
    event["note"] = {f"k {token}": [f"v {token}"]}
    result = {"type": "result", "is_error": True, "result": f"bad token {token}"}
    stdout = json.dumps(event) + "\n" + json.dumps(result) + "\n"
    rec = to_record(T0, T1, CliRun(None, stdout, f"x {token}", 5, True), token)
    assert rec.error == "timeout"
    stored = json.dumps([rec.detail, rec.raw, rec.cli_version])
    assert token not in stored
    error = to_record(T0, T1, CliRun(1, json.dumps(result), "", 5, False), token)
    assert error.detail == "bad token [redacted]"


def test_redact_empty_secret():
    assert redact("abc", "") == "abc"


def test_redact_every_occurrence():
    assert redact("a tok b tok", "tok") == "a [redacted] b [redacted]"


def test_redact_cut_token_at_the_start():
    # stderr is kept from its tail, so the kept text may start inside the token
    assert redact("en-123 then more", "fake-token-123") == "[redacted] then more"
    assert redact("23 then", "fake-token-123") == "23 then"


def test_detail_capped():
    stderr = "".join(chr(ord("a") + i % 26) for i in range(10000))
    rec = to_record(T0, T1, CliRun(1, "", stderr, 5, False), TOKEN)
    assert len(rec.detail) <= 2000
    assert rec.detail == stderr[-2000:]
    wide = "\u017c" * 3000
    capped = to_record(T0, T1, CliRun(1, "", wide, 5, False), TOKEN).detail
    assert len(capped) == 2000 and capped == wide[-2000:]


def test_empty_stderr_has_no_detail():
    rec = to_record(T0, T1, CliRun(3, "", "", 5, False), TOKEN)
    assert (rec.error, rec.detail) == ("cli_exit", None)


async def test_hung_child_record(tmp_path):
    env = {"FAKE_MODE": "hang", "FAKE_PID_FILE": str(tmp_path / "pid")}
    run = await run_cli([sys.executable, FAKE], env, str(tmp_path), 1)
    rec = to_record(T0, T1, run, TOKEN)
    assert (rec.ok, rec.error, rec.windows) == (False, "timeout", {})


async def test_token_echoing_child_record(tmp_path):
    env = {"FAKE_MODE": "echo-token", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    run = await run_cli([sys.executable, FAKE], env, str(tmp_path), 30)
    rec = to_record(T0, T1, run, TOKEN)
    assert (rec.ok, rec.error) == (False, "cli_exit")
    assert TOKEN not in rec.detail and rec.detail.count("[redacted]") == 2


async def test_not_logged_in_child_record(tmp_path):
    env = {"FAKE_MODE": "not-logged-in", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    run = await run_cli([sys.executable, FAKE], env, str(tmp_path), 30)
    rec = to_record(T0, T1, run, TOKEN)
    assert (rec.ok, rec.error, rec.detail) == (False, "cli_error", "Not logged in")
