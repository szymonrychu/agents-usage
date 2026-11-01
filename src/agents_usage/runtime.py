"""Process entry points: the api server and the single-instance worker helper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator

import uvicorn
from agents_common.db import open_pool
from agents_common.runtime import advisory_singleton, run_service
from agents_common.service_gate import load_tokens
from psycopg_pool import AsyncConnectionPool
from starlette.types import ASGIApp

from agents_usage.api.app import create_app, create_ingest_app, create_service_app
from agents_usage.cli import EXIT_CONFIG
from agents_usage.settings import Settings

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

SingletonBody = Callable[[Settings, AsyncConnectionPool, asyncio.Event], Awaitable[None]]


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level.upper(), format=LOG_FORMAT, force=True)


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: run_service owns SIGTERM and SIGINT."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _api_config(settings: Settings, app: ASGIApp, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=settings.http_host,
        port=port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        proxy_headers=False,  # the allowlist must see the socket peer, never X-Forwarded-For
    )


async def _serve(
    settings: Settings, stop: asyncio.Event, tokens: dict[str, list[str]] | None = None
) -> None:
    """Serve the ui listener plus the service and ingest listeners whose token lists are given."""
    tokens = tokens or {}
    pool = await open_pool(settings.database_url.get_secret_value(), min_size=1, max_size=10)
    try:
        servers = [_Server(_api_config(settings, create_app(settings, pool), settings.http_port))]
        if "service" in tokens:
            service = create_service_app(settings, pool, tokens["service"])
            servers.append(_Server(_api_config(settings, service, settings.service_port)))
        if "ingest" in tokens:
            ingest = create_ingest_app(settings, pool, tokens["ingest"])
            servers.append(_Server(_api_config(settings, ingest, settings.ingest_port)))
        serving = [asyncio.create_task(server.serve()) for server in servers]
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({*serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        stopping.cancel()
        await asyncio.gather(*serving)
    finally:
        await pool.close()


def _listener_tokens(settings: Settings) -> dict[str, list[str]]:
    """The token list of each enabled token listener; ValueError names the setting at fault.

    The ingest tokens (the agents') must never also open the service port (the read side).
    """
    tokens: dict[str, list[str]] = {}
    for name, var, path in (
        ("service", "USAGE_SERVICE_TOKENS_FILE", settings.service_tokens_file),
        ("ingest", "USAGE_INGEST_TOKENS_FILE", settings.ingest_tokens_file),
    ):
        if not path:
            continue
        try:
            tokens[name] = load_tokens(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{var}: {exc}") from None
    if set(tokens.get("service", ())) & set(tokens.get("ingest", ())):
        raise ValueError(
            "USAGE_INGEST_TOKENS_FILE: ingest tokens must differ from the service tokens"
        )
    return tokens


def api_main(settings: Settings) -> int:
    """Serve the http listeners until SIGTERM or SIGINT; the exit code of the process."""
    configure_logging(settings)
    try:
        tokens = _listener_tokens(settings)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    return asyncio.run(
        run_service(
            lambda stop: _serve(settings, stop, tokens),
            grace_seconds=settings.shutdown_grace_seconds,
        )
    )


async def _hold_and_run(
    settings: Settings,
    pool: AsyncConnectionPool,
    lock_name: str,
    body: SingletonBody,
    stop: asyncio.Event,
    holding: asyncio.Event,
) -> None:
    log.info("waiting for the lock %s", lock_name)
    async with advisory_singleton(pool, lock_name):
        holding.set()
        log.info("holds the lock %s", lock_name)
        await body(settings, pool, stop)


async def _work(settings: Settings, lock_name: str, body: SingletonBody, stop: asyncio.Event):
    pool = await open_pool(settings.database_url.get_secret_value(), min_size=2, max_size=10)
    holding = asyncio.Event()
    try:
        holder = asyncio.create_task(_hold_and_run(settings, pool, lock_name, body, stop, holding))
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({holder, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if not holder.done() and not holding.is_set():
            holder.cancel()  # still queued for the lock: nothing to stop
        with contextlib.suppress(asyncio.CancelledError):
            await holder
    finally:
        await pool.close()


def _run_singleton(settings: Settings, lock_name: str, body: SingletonBody) -> int:
    """Run `body(settings, pool, stop)` while holding the advisory lock `lock_name`.

    One copy of the lock boilerplate for every single-instance worker (the prober and the
    watcher): waits for the lock, returns when `stop` is set (SIGTERM or SIGINT) after giving
    `body` the grace period, and returns the process exit code.
    """
    configure_logging(settings)
    return asyncio.run(
        run_service(
            lambda stop: _work(settings, lock_name, body, stop),
            grace_seconds=settings.shutdown_grace_seconds,
        )
    )
