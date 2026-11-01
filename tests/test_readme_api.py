"""README API coverage and the committed OpenAPI document."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import DownPool
from fastapi.routing import APIRoute

from agents_usage.api import openapi
from agents_usage.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_usage.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
SETTINGS = Settings(database_url="postgresql://unused/db")

READ_ROUTES = {"GET /usage/v1/current", "GET /usage/v1/history"}
INGEST_ROUTES = {"POST /usage/v1/samples"}


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _walk(router, prefix: str = ""):
    """(path, methods) of every route; FastAPI includes routers lazily, so descend into them."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def routes_of(app) -> set[str]:
    out = set()
    for path, methods in _walk(app):
        if path.startswith(PREFIX):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes() -> set[str]:
    found = re.findall(
        r"^### `(GET|POST|PATCH|PUT|DELETE) (/usage/v1\S*)`\s*$", section("API"), re.MULTILINE
    )
    return {f"{m} {p}" for m, p in found}


def test_listeners_serve_their_routes() -> None:
    assert routes_of(create_app(SETTINGS, DownPool())) == READ_ROUTES
    assert routes_of(create_service_app(SETTINGS, DownPool(), ["t"])) == READ_ROUTES
    assert routes_of(create_ingest_app(SETTINGS, DownPool(), ["i"])) == INGEST_ROUTES


def test_openapi_matches_app() -> None:
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agents_usage.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")
    paths = {p for p in json.loads(committed)["paths"] if p.startswith(PREFIX)}
    assert paths == {r.split(" ", 1)[1] for r in READ_ROUTES | INGEST_ROUTES}


def test_every_route_documented() -> None:
    expected = READ_ROUTES | INGEST_ROUTES
    missing = expected - readme_routes()
    assert not missing, f"undocumented routes: {sorted(missing)}"
    stale = readme_routes() - expected
    assert not stale, f"README documents routes the app lacks: {sorted(stale)}"


def test_readme_documents_the_read_contract() -> None:
    api = section("API")
    for needle in (
        "/usage/v1",
        "windows",
        "last_sample",
        "last_probe",
        "resets_at",
        "observed_at",
        "stale",
        "statusline",
        "probe",
        "bucket_seconds",
        "max_points",
        "USAGE_STALE_SECONDS",
        "USAGE_PROBE_AFTER_SECONDS",
        "USAGE_ALLOWED_CIDRS",
        "unauthorized",
        "forbidden",
        "invalid_request",
        "too_large",
        "no_windows",
    ):
        assert needle in api, needle
