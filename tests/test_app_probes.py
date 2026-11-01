"""Probes and listener guards; no database: the pool is a stub."""

import pytest
from conftest import DownPool, asgi_client

from agents_usage.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_usage.api.usage import ProbeTrigger


@pytest.fixture
def ui(settings):
    return create_app(settings, DownPool())


async def test_healthz_ok_without_db(ui):
    async with asgi_client(ui) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_503_when_db_down(ui):
    async with asgi_client(ui) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"


async def test_ui_app_refuses_foreign_peer(ui):
    async with asgi_client(ui, peer="10.0.0.5") as http:
        refused = await http.get(f"{PREFIX}/x")
        health = await http.get("/healthz")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert health.status_code == 200


async def test_ui_app_serves_allowed_peer(ui):
    async with asgi_client(ui) as http:
        response = await http.get(f"{PREFIX}/x")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_service_app_needs_token(settings):
    app = create_service_app(settings, DownPool(), ["t"])
    async with asgi_client(app, peer="10.9.9.9") as http:
        anonymous = await http.get(f"{PREFIX}/x")
        wrong = await http.get(f"{PREFIX}/x", headers={"Authorization": "Bearer nope"})
        known = await http.get(f"{PREFIX}/x", headers={"Authorization": "Bearer t"})
        health = await http.get("/healthz")
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert known.status_code == 404
    assert health.status_code == 200


def test_probe_trigger_only_on_the_service_app(settings):
    assert create_app(settings, DownPool()).state.probe_trigger is None
    service = create_service_app(settings, DownPool(), ["t"])
    assert isinstance(service.state.probe_trigger, ProbeTrigger)
    assert service.state.probe_trigger.after.total_seconds() == settings.probe_after_seconds


async def test_ingest_app_needs_token_but_not_health(settings):
    app = create_ingest_app(settings, DownPool(), ["i"])
    async with asgi_client(app, peer="10.9.9.9") as http:
        anonymous = await http.get(f"{PREFIX}/x")
        known = await http.get(f"{PREFIX}/x", headers={"Authorization": "Bearer i"})
        health = await http.get("/healthz")
        ready = await http.get("/readyz")
    assert anonymous.status_code == 401
    assert known.status_code == 404
    assert health.status_code == 200
    assert ready.status_code == 503


def test_no_probe_trigger_on_the_ingest_app(settings):
    assert create_ingest_app(settings, DownPool(), ["i"]).state.probe_trigger is None
