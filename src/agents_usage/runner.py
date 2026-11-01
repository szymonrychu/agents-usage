"""The probe's claude child: its argv, its environment and a bounded process runner.

The OAuth token goes into the child's environment and nowhere else: never into argv (visible
in the process list), never into a log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass

from agents_usage.settings import Settings

PROBE_PROMPT = "Reply with the single word ok."
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
STDOUT_CAP = 1024 * 1024
STDERR_CAP = 64 * 1024
# After the child exits (or is killed) its pipes close; a grandchild that left the process
# group could keep them open, so the reads are given this long and then dropped.
PIPE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class CliRun:
    returncode: int | None  # None when the run timed out and the group was killed
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


def probe_argv(settings: Settings) -> list[str]:
    """No tools, no MCP servers, no session file, a one-line system prompt: the cheapest run
    that still makes one API request (which is what returns the rate limit event)."""
    return [
        settings.claude_bin,
        "-p",
        PROBE_PROMPT,
        "--model",
        settings.claude_model,
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


def probe_env(settings: Settings, token: str) -> dict[str, str]:
    """The child environment, built from scratch: nothing of the parent but PATH."""
    return {
        "PATH": os.environ.get("PATH") or DEFAULT_PATH,
        "HOME": settings.claude_home,
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "DISABLE_AUTOUPDATER": "1",
    }


class _Tail:
    """Reads a pipe to EOF keeping only its last `cap` bytes (the stream-json result and
    rate limit lines come last); what was read survives a cancelled read."""

    def __init__(self, stream: asyncio.StreamReader | None, cap: int) -> None:
        self.data = bytearray()
        self.cap = cap
        self.task = asyncio.ensure_future(self._read(stream))

    async def _read(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            self.data += chunk
            if len(self.data) > self.cap:
                del self.data[: len(self.data) - self.cap]

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def run_cli(argv: list[str], env: dict[str, str], cwd: str, timeout: float) -> CliRun:
    """Run `argv` in its own session (process group) with exactly `env`, in `cwd` (created
    0700 if missing), for at most `timeout` seconds.

    On timeout, and on cancellation, the whole group is SIGKILLed. Stragglers left in the group
    after the child exits are killed too, so nothing holding the token outlives the run.
    """
    os.makedirs(cwd, mode=0o700, exist_ok=True)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out = _Tail(proc.stdout, STDOUT_CAP)
    err = _Tail(proc.stderr, STDERR_CAP)
    timed_out = False
    try:
        try:
            async with asyncio.timeout(timeout):
                await proc.wait()
        except TimeoutError:
            timed_out = True
        _kill_group(proc.pid)
        await proc.wait()
        await asyncio.wait({out.task, err.task}, timeout=PIPE_GRACE_SECONDS)
    finally:
        if proc.returncode is None:
            _kill_group(proc.pid)
        for tail in (out, err):
            tail.task.cancel()
    duration_ms = int((time.monotonic() - started) * 1000)
    return CliRun(
        None if timed_out else proc.returncode, out.text(), err.text(), duration_ms, timed_out
    )
