"""The service commands as real processes and real listeners.

The ones taking `db_url` or `empty_db_url` need Postgres (testcontainers): CI is their evidence.
"""

import asyncio
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from agents_common.runtime import lock_key
from conftest import DownPool

from agents_usage import runtime
from agents_usage.api.app import create_app
from agents_usage.db.probes import PROBE_CHANNEL

FAKE = Path(__file__).parent / "fake_claude.py"
STARTED = f"holds the lock {runtime.PROBER_LOCK}"
STANDBY = f"waiting for the lock {runtime.PROBER_LOCK}"
LISTENING = f"waiting for probe requests on {PROBE_CHANNEL}"


def env_for(url: str | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("USAGE_")}
    if url is not None:
        env["USAGE_DATABASE_URL"] = url
    env.update(USAGE_SHUTDOWN_GRACE_SECONDS="10", PYTHONUNBUFFERED="1", **extra)
    return env


def usage(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agents_usage.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait(check: Callable[[], object], timeout: float = 30, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Process:
    """A `usage <command>` process with its stdout and stderr in one log file."""

    def __init__(self, command: str, env: dict[str, str], log: Path) -> None:
        self.log = log
        self.out = log.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agents_usage.cli", command],
            env=env,
            stdout=self.out,
            stderr=subprocess.STDOUT,
        )

    def text(self) -> str:
        return self.log.read_text()

    def wait_for(self, needle: str, timeout: float = 30) -> None:
        wait(lambda: needle in self.text(), timeout, f"{needle!r} in {self.text()!r}")

    def stop(self) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=30)


@pytest.fixture
def processes(tmp_path: Path) -> Iterator[Callable[..., Process]]:
    started: list[Process] = []

    def start(name: str, command: str, env: dict[str, str]) -> Process:
        process = Process(command, env, tmp_path / f"{name}.log")
        started.append(process)
        return process

    yield start
    for process in started:
        if process.proc.poll() is None:
            process.proc.kill()
            process.proc.wait()
        process.out.close()


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """An executable `claude` that replays the haiku fixture (FAKE_MODE is not in the probe
    env, so the fake runs its default fixture mode)."""
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
    )
    path.chmod(0o755)
    return path


def prober_env(url: str, tmp_path: Path, claude: Path) -> dict[str, str]:
    token = tmp_path / "claude-token"
    token.write_text("fake-oauth-token\n")
    return env_for(
        url,
        USAGE_CLAUDE_BIN=str(claude),
        USAGE_CLAUDE_HOME=str(tmp_path / "claude-home"),
        USAGE_CLAUDE_TOKEN_FILE=str(token),
    )


def scalar(url: str, query: str, params: tuple = ()) -> object:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def lock_holders(url: str) -> int:
    return int(
        scalar(
            url,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND ((classid::bigint << 32) | objid::bigint) = %s",
            (lock_key(runtime.PROBER_LOCK),),
        )
    )


def test_help_lists_commands():
    result = usage("--help", env=env_for(None))
    assert result.returncode == 0
    for command in ("migrate", "api", "prober"):
        assert command in result.stdout


def test_bad_config_exits_78():
    env = env_for(None, USAGE_STALE_SECONDS="1")
    for command in ("migrate", "api", "prober"):
        result = usage(command, env=env)
        assert result.returncode == 78, (command, result.stderr)
        assert "USAGE_DATABASE_URL" in result.stderr
        assert "USAGE_STALE_SECONDS" in result.stderr


def test_prober_without_token_file_exits_78():
    result = usage("prober", env=env_for("postgresql://u:p@db.invalid/usage"))
    assert result.returncode == 78
    assert "USAGE_CLAUDE_TOKEN_FILE: required for the prober" in result.stderr


@pytest.mark.parametrize(
    ("cidrs", "expected"),
    [("127.0.0.1/32", 404), ("203.0.113.9/32", 403)],
)
async def test_api_ignores_forwarded_headers(settings_for, cidrs, expected):
    """Behind the oauth2-proxy sidecar the allowlist sees the socket peer, not X-Forwarded-For:
    a forwarded allowed address neither opens nor closes the door."""
    port = free_port()
    settings = settings_for(
        "postgresql://u:p@db.invalid/usage",
        http_host="127.0.0.1",
        http_port=port,
        allowed_cidrs=[cidrs],
    )
    server = runtime._Server(runtime._api_config(settings, create_app(settings, DownPool()), port))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            forwarded = await http.get(
                "/usage/v1/x",
                headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9"},
            )
        assert forwarded.status_code == expected, forwarded.text
    finally:
        server.should_exit = True
        await serving


def test_migrate_command(empty_db_url: str):
    result = usage("migrate", env=env_for(empty_db_url))
    assert result.returncode == 0, result.stderr
    assert "0001_init.sql" in result.stdout
    assert scalar(empty_db_url, "SELECT to_regclass('public.probes')::text") == "probes"
    again = usage("migrate", env=env_for(empty_db_url))
    assert again.returncode == 0
    assert "0001_init.sql" not in again.stdout


def test_api_serves_on_http_port_and_stops_on_sigterm(db_url: str, processes):
    port = free_port()
    env = env_for(db_url, USAGE_HTTP_PORT=str(port), USAGE_HTTP_HOST="127.0.0.1")
    api = processes("api", "api", env)

    def healthy() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    wait(healthy, what="the api to answer /readyz")
    assert api.stop() == 0


async def test_token_listeners_serve_behind_their_tokens(db_url: str, settings_for):
    ui_port, service_port, ingest_port = free_port(), free_port(), free_port()
    settings = settings_for(
        db_url,
        http_host="127.0.0.1",
        http_port=ui_port,
        service_port=service_port,
        service_tokens_file="/unused",
        ingest_port=ingest_port,
        ingest_tokens_file="/unused",
    )
    tokens = {"service": ["svc-test-token"], "ingest": ["ingest-test-token"]}
    stop = asyncio.Event()
    serving = asyncio.create_task(runtime._serve(settings, stop, tokens))
    window = {"used_percentage": 23.5, "resets_at": 1794020400}
    body = {"agent": "agent-x-0", "cli_version": "2.1.304", "rate_limits": {"five_hour": window}}
    try:
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{ui_port}") as ui,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{service_port}") as service,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{ingest_port}") as ingest,
        ):
            for http in (ui, service, ingest):
                for _ in range(100):
                    try:
                        if (await http.get("/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.05)
            svc = {"Authorization": "Bearer svc-test-token"}
            ing = {"Authorization": "Bearer ingest-test-token"}
            assert (await ui.get("/usage/v1/x")).status_code == 404
            assert (await service.get("/usage/v1/x")).status_code == 401
            assert (await service.get("/usage/v1/x", headers=svc)).status_code == 404
            assert (
                await ingest.post("/usage/v1/samples", json=body, headers=svc)
            ).status_code == 401
            posted = await ingest.post("/usage/v1/samples", json=body, headers=ing)
            assert posted.status_code == 202, posted.text
            assert (await ui.post("/usage/v1/samples", json=body)).status_code == 404
    finally:
        stop.set()
        await serving


def test_standby_prober_takes_over(db_url: str, tmp_path: Path, fake_claude: Path, processes):
    env = prober_env(db_url, tmp_path, fake_claude)
    first = processes("first", "prober", env)
    first.wait_for(STARTED)
    second = processes("second", "prober", env)
    second.wait_for(STANDBY)
    assert STARTED not in second.text()
    assert lock_holders(db_url) == 1
    assert first.stop() == 0
    second.wait_for(STARTED)
    second.wait_for(LISTENING)
    assert lock_holders(db_url) == 1
    assert second.stop() == 0
    wait(lambda: lock_holders(db_url) == 0, what="the lock to be released")


def test_prober_probes_only_when_notified(
    db_url: str, tmp_path: Path, fake_claude: Path, processes
):
    def probes() -> int:
        return int(scalar(db_url, "SELECT count(*) FROM probes"))

    def notify() -> None:
        scalar(db_url, "SELECT pg_notify(%s, '')", (PROBE_CHANNEL,))

    worker = processes("prober", "prober", prober_env(db_url, tmp_path, fake_claude))
    worker.wait_for(LISTENING)
    time.sleep(1.5)
    assert probes() == 0  # no timer: nothing asked, nothing ran

    def notified_probe() -> bool:
        # repeated: a notification sent before LISTEN is in place is lost
        notify()
        time.sleep(0.3)
        return probes() >= 1

    wait(notified_probe, what="a probe after a notification")
    for _ in range(3):
        notify()
        time.sleep(0.2)
    time.sleep(1)
    assert probes() == 1  # the fresh probe makes the next requests not due
    assert scalar(db_url, "SELECT ok FROM probes") is True
    assert scalar(db_url, "SELECT source || ':' || metric FROM readings") == "probe:seven_day"
    home = tmp_path / "claude-home"
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert "fake-oauth-token" not in worker.text()
    assert worker.stop() == 0
