"""The probe's claude child: argv, environment and the process runner (no database)."""

import json
import stat
import sys
import time
from pathlib import Path

from agents_usage.parser import parse_stream
from agents_usage.runner import (
    PROBE_PROMPT,
    STDERR_CAP,
    STDOUT_CAP,
    probe_argv,
    probe_env,
    run_cli,
)

FAKE = str(Path(__file__).parent / "fake_claude.py")
TOKEN = "fake-oauth-token"


def fake(mode: str, **extra: str) -> tuple[list[str], dict[str, str]]:
    return [sys.executable, FAKE], {"FAKE_MODE": mode, **extra}


def gone(pid: int) -> bool:
    """True when the process no longer exists or is a zombie nobody reaped yet."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return True
    return state in ("Z", "X")


def test_argv_exact(settings):
    assert probe_argv(settings) == [
        "claude",
        "-p",
        PROBE_PROMPT,
        "--model",
        "haiku",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt",
        PROBE_PROMPT,
    ]
    assert PROBE_PROMPT == "Reply with the single word ok."


def test_argv_uses_configured_bin_and_model(settings_for):
    custom = settings_for(
        "postgresql://u:p@db.invalid/usage", claude_bin="/opt/c", claude_model="m"
    )
    argv = probe_argv(custom)
    assert argv[0] == "/opt/c"
    assert argv[argv.index("--model") + 1] == "m"


def test_env_has_only_four_keys(settings, monkeypatch):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("PATH", "/x/bin:/usr/bin")
    env = probe_env(settings, "tok")
    assert set(env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER"}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and env["HOME"] == settings.claude_home
    assert env["PATH"] == "/x/bin:/usr/bin"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "postgresql://secret" not in env.values()


def test_env_default_path(settings, monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert probe_env(settings, "tok")["PATH"] == "/usr/local/bin:/usr/bin:/bin"


async def test_run_cli_captures_stdout(tmp_path):
    argv, env = fake("fixture")
    run = await run_cli(argv, env, str(tmp_path), 30)
    assert run.returncode == 0
    assert run.timed_out is False
    assert run.stderr == ""
    assert run.duration_ms >= 0
    parsed = parse_stream(run.stdout)
    assert parsed.event is not None
    assert parsed.windows["seven_day"].percent == 22.0


async def test_run_cli_passes_argv_and_only_the_given_env(tmp_path):
    argv, env = fake("echo", CLAUDE_CODE_OAUTH_TOKEN=TOKEN)
    run = await run_cli([*argv, "--tools", "", "-p", "x"], env, str(tmp_path), 30)
    seen = json.loads(run.stdout)
    assert seen["argv"] == ["--tools", "", "-p", "x"]
    assert seen["env"] == sorted(env)


async def test_run_cli_creates_cwd_private(tmp_path):
    home = tmp_path / "claude-home"
    argv, env = fake("fixture")
    run = await run_cli(argv, env, str(home), 30)
    assert run.returncode == 0
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


async def test_run_cli_timeout_kills_group(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    argv, env = fake("hang", FAKE_PID_FILE=str(pid_file))
    started = time.monotonic()
    run = await run_cli(argv, env, str(tmp_path), 1)
    assert time.monotonic() - started < 10
    assert run.timed_out is True
    assert run.returncode is None
    assert run.duration_ms >= 1000
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while not gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert gone(grandchild)


async def test_run_cli_caps_stdout(tmp_path):
    argv, env = fake("flood")
    run = await run_cli(argv, env, str(tmp_path), 30)
    assert run.returncode == 0
    assert 0 < len(run.stdout) <= STDOUT_CAP
    assert STDOUT_CAP == 1024 * 1024 and STDERR_CAP == 64 * 1024


async def test_run_cli_nonzero_exit(tmp_path):
    argv, env = fake("echo-token", CLAUDE_CODE_OAUTH_TOKEN=TOKEN)
    run = await run_cli(argv, env, str(tmp_path), 30)
    assert run.returncode == 1
    assert run.timed_out is False
    assert "auth failed" in run.stderr
