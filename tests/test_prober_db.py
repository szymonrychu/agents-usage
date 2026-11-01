"""probe_once and handle_request against Postgres (CI only: needs Docker)."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents_usage import prober
from agents_usage.runner import PROBE_PROMPT, CliRun

HAIKU = (Path(__file__).parent / "fixtures" / "haiku-2.1.304.jsonl").read_text()
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TOKEN = "fake-oauth-token"


class FakeRun:
    """Records each call to the claude runner and answers with a fixed CliRun or error."""

    def __init__(self, result: CliRun | None = None, error: Exception | None = None) -> None:
        self.result = result or CliRun(0, HAIKU, "", 1934, False)
        self.error = error
        self.calls: list[tuple[list[str], dict[str, str], str, float]] = []

    async def __call__(self, argv, env, cwd, timeout) -> CliRun:
        self.calls.append((argv, env, cwd, timeout))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "claude-token"
    path.write_text(TOKEN + "\n")
    return path


@pytest.fixture
def probe_settings(settings_for, db_url, token_file, tmp_path):
    return settings_for(
        db_url, claude_token_file=str(token_file), claude_home=str(tmp_path / "home")
    )


async def rows(pool, query: str) -> list[tuple]:
    async with pool.connection() as conn:
        return await (await conn.execute(query)).fetchall()


async def test_probe_once_stores_the_reading(pool, probe_settings):
    run = FakeRun()
    probe_id = await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    argv, env, cwd, timeout = run.calls[0]
    assert argv[argv.index("-p") + 1] == PROBE_PROMPT
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert cwd == probe_settings.claude_home and timeout == 120
    assert await rows(pool, "SELECT id, ok, error FROM probes") == [(probe_id, True, None)]
    assert await rows(pool, "SELECT source, metric, percent FROM readings") == [
        ("probe", "seven_day", 22.0)
    ]


async def test_handle_request_burst_one_probe(pool, probe_settings):
    run = FakeRun()
    first = await prober.handle_request(pool, probe_settings, run=run, now=lambda: NOW)
    second = await prober.handle_request(pool, probe_settings, run=run, now=lambda: NOW)
    assert (first, second) == (True, False)
    assert len(run.calls) == 1
    assert await rows(pool, "SELECT count(*) FROM probes") == [(1,)]


async def test_failed_probe_also_blocks_the_next(pool, probe_settings):
    run = FakeRun(CliRun(1, "", "boom", 5, False))
    assert await prober.handle_request(pool, probe_settings, run=run, now=lambda: NOW)
    assert not await prober.handle_request(pool, probe_settings, run=run, now=lambda: NOW)
    assert len(run.calls) == 1


async def test_probe_once_reads_token_file_each_time(pool, probe_settings, token_file):
    run = FakeRun()
    await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    token_file.write_text("fake-oauth-token-rotated\n")
    await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    seen = [env["CLAUDE_CODE_OAUTH_TOKEN"] for _, env, _, _ in run.calls]
    assert seen == [TOKEN, "fake-oauth-token-rotated"]


async def test_probe_once_survives_run_exception(pool, probe_settings):
    run = FakeRun(error=OSError("claude not found"))
    await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    [(ok, error, detail)] = await rows(pool, "SELECT ok, error, detail FROM probes")
    assert (ok, error) == (False, "cli_exit")
    assert detail == "OSError: claude not found"


@pytest.mark.parametrize("content", ["", "  \n", None])
async def test_empty_token_file(pool, probe_settings, token_file, content, caplog):
    if content is None:
        token_file.unlink()
    else:
        token_file.write_text(content)
    run = FakeRun()
    with caplog.at_level(logging.INFO, logger="agents_usage.prober"):
        await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    assert run.calls == []
    assert await rows(pool, "SELECT ok, error, detail FROM probes") == [
        (False, "cli_error", "claude token file unreadable or empty")
    ]
    assert "claude token file unreadable or empty" in caplog.text


async def test_token_never_stored_or_logged(pool, probe_settings, caplog):
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {"rateLimitType": "seven_day", "utilization": 0.5, "note": TOKEN},
    }
    stdout = json.dumps(event) + "\n"
    run = FakeRun(CliRun(None, stdout, f"auth failed for {TOKEN}", 120000, True))
    with caplog.at_level(logging.DEBUG):
        await prober.probe_once(pool, probe_settings, run=run, now=lambda: NOW)
    [(error, detail, raw)] = await rows(pool, "SELECT error, detail, raw::text FROM probes")
    assert error == "timeout"
    assert TOKEN not in detail and "[redacted]" in detail
    assert TOKEN not in raw
    assert TOKEN not in caplog.text
    assert "probe" in caplog.text and "timeout" in caplog.text
