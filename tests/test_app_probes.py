"""Probes and listener guards; no database: the pool is a stub."""

import pytest
from conftest import asgi_client

from agents_usage.api.app import PREFIX, create_app, create_service_app


class DownPool:
    def connection(self):
        raise RuntimeError("database is down")


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
    assert create_app(settings, DownPool()).state.probe_trigger is False
    assert create_service_app(settings, DownPool(), ["t"]).state.probe_trigger is True
