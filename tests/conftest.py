import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import pytest_asyncio
from agents_common.db import open_pool
from fastapi import FastAPI
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agents_usage.api.app import create_app, create_ingest_app, create_service_app
from agents_usage.api.ingest import IngestState
from agents_usage.db.samples import SampleRecord
from agents_usage.settings import Settings

PACKAGE = "agents_usage"
SERVICE_TOKEN = "t"
INGEST_TOKEN = "i"
FAKE_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
UI_PEER = "127.0.0.1"


@pytest.fixture
def db_url(migrated_db: Callable[[str], str]) -> str:
    """A fresh database cloned from the migrated template."""
    return migrated_db(PACKAGE)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        yield connection


@pytest_asyncio.fixture
async def aconn(db_url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as connection:
        yield connection


@pytest_asyncio.fixture
async def pool(db_url: str) -> AsyncIterator[AsyncConnectionPool]:
    opened = await open_pool(db_url, min_size=1, max_size=4)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
def empty_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database with no schema at all."""
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield f"{pg_url.rsplit('/', 1)[0]}/{name}"
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def settings_for() -> Callable[..., Settings]:
    """Settings for a database url; keyword overrides replace any field."""

    def make(url: str, **overrides: object) -> Settings:
        return Settings(database_url=url, **overrides)

    return make


@pytest.fixture
def settings(settings_for: Callable[..., Settings]) -> Settings:
    """Settings that need no database."""
    return settings_for("postgresql://u:p@db.invalid/usage")


def asgi_client(app: FastAPI, peer: str = UI_PEER) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 50000)), base_url="http://usage"
    )


@pytest_asyncio.fixture
async def client(settings: Settings, pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    """The UI app over the migrated database, called from the allowed peer 127.0.0.1."""
    async with asgi_client(create_app(settings, pool)) as http:
        yield http


@pytest_asyncio.fixture
async def client_service(
    settings: Settings, pool: AsyncConnectionPool
) -> AsyncIterator[httpx.AsyncClient]:
    """The service app over the migrated database; the bearer token is `t`."""
    app = create_service_app(settings, pool, [SERVICE_TOKEN])
    async with asgi_client(app) as http:
        http.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
        yield http


class DownPool:
    """A pool stub for the apps whose tests need no database."""

    def connection(self):
        raise RuntimeError("database is down")


class FakeSamples:
    """Records the SampleRecords and prunes instead of touching a database."""

    def __init__(self) -> None:
        self.records: list[SampleRecord] = []
        self.pruned: list[datetime] = []
        self.prune_error: Exception | None = None
        self.insert_error: Exception | None = None

    async def insert(self, rec: SampleRecord) -> int:
        if self.insert_error:
            raise self.insert_error
        self.records.append(rec)
        return len(self.records)

    async def prune(self, older_than: datetime) -> int:
        self.pruned.append(older_than)
        if self.prune_error:
            raise self.prune_error
        return 0


@pytest.fixture
def fake_samples() -> FakeSamples:
    return FakeSamples()


@pytest.fixture
def ingest_app(settings: Settings, fake_samples: FakeSamples) -> FastAPI:
    """The ingest app (ingest token `i`) over a recording store and a fixed clock; no database."""
    app = create_ingest_app(settings, DownPool(), [INGEST_TOKEN])
    app.state.ingest = IngestState(fake_samples, app.state.ingest.retention, lambda: FAKE_NOW)
    return app


@pytest_asyncio.fixture
async def client_ingest(ingest_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The ingest app called from a pod address with the ingest bearer token."""
    async with asgi_client(ingest_app, peer="10.9.9.9") as http:
        http.headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
        yield http
