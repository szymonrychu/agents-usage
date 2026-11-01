"""The ingest listener; no database: the store is a recording fake."""

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FAKE_NOW, DownPool, asgi_client
from psycopg.errors import UntranslatableCharacter

from agents_usage import runtime
from agents_usage.api.app import create_app, create_service_app

FIXTURES = Path(__file__).parent / "fixtures"
RATE_LIMITS = json.loads((FIXTURES / "statusline-rate-limits.json").read_text())
BODY = {"agent": "agent-x-0", "cli_version": "2.1.304", "rate_limits": RATE_LIMITS}
URL = "/usage/v1/samples"


async def test_accepts_sample(client_ingest, fake_samples):
    response = await client_ingest.post(URL, json=BODY)
    assert response.status_code == 202
    assert response.json() == {"windows": 2}
    (rec,) = fake_samples.records
    assert {m: w.percent for m, w in rec.windows.items()} == {"five_hour": 23.5, "seven_day": 22.0}
    assert rec.raw == RATE_LIMITS
    assert (rec.agent, rec.cli_version) == ("agent-x-0", "2.1.304")
    assert rec.observed_at == FAKE_NOW


async def test_agent_and_cli_version_are_optional(client_ingest, fake_samples):
    response = await client_ingest.post(URL, json={"rate_limits": RATE_LIMITS})
    assert response.status_code == 202
    assert (fake_samples.records[0].agent, fake_samples.records[0].cli_version) == (None, None)


async def test_needs_ingest_token(ingest_app):
    async with asgi_client(ingest_app, peer="10.9.9.9") as http:
        anonymous = await http.post(URL, json=BODY)
        service_token = await http.post(URL, json=BODY, headers={"Authorization": "Bearer t"})
        ingest_token = await http.post(URL, json=BODY, headers={"Authorization": "Bearer i"})
    assert anonymous.status_code == 401
    assert service_token.status_code == 401
    assert ingest_token.status_code == 202


@pytest.mark.parametrize("rate_limits", [{}, {"spend_limit": {"used_usd": 1}}])
async def test_no_valid_window_422(client_ingest, fake_samples, rate_limits):
    response = await client_ingest.post(URL, json={"rate_limits": rate_limits})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_windows"
    assert fake_samples.records == []


async def test_unknown_field_422(client_ingest, fake_samples):
    response = await client_ingest.post(URL, json={**BODY, "percent": 1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert fake_samples.records == []


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", b'{"rate_limits": []}'])
async def test_malformed_body_422(client_ingest, content):
    response = await client_ingest.post(URL, content=content)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_non_finite_raw_422(client_ingest, fake_samples):
    content = (
        b'{"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": 1794020400}, "x": NaN}}'
    )
    response = await client_ingest.post(URL, content=content)
    assert response.status_code == 422
    assert fake_samples.records == []


async def test_unstorable_raw_422(client_ingest, fake_samples):
    fake_samples.insert_error = UntranslatableCharacter("unsupported Unicode escape")
    response = await client_ingest.post(URL, json=BODY)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def _padded(size: int) -> bytes:
    """A valid body of exactly `size` bytes (padded with spaces)."""
    body = json.dumps(BODY).encode()
    return body + b" " * (size - len(body))


async def test_body_cap(client_ingest, fake_samples):
    big = _padded(8193)
    declared = await client_ingest.post(URL, content=big)
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "too_large"

    async def chunks():
        for i in range(0, len(big), 1000):
            yield big[i : i + 1000]

    chunked = await client_ingest.post(URL, content=chunks())
    assert chunked.status_code == 413
    assert chunked.json()["error"]["code"] == "too_large"
    assert fake_samples.records == []


async def test_body_cap_is_exact(client_ingest):
    assert (await client_ingest.post(URL, content=_padded(8192))).status_code == 202
    assert (await client_ingest.post(URL, content=_padded(8193))).status_code == 413


@pytest.mark.parametrize("agent", ["a b", "a" * 129, "", "ab\n"])
async def test_bad_agent_422(client_ingest, agent):
    response = await client_ingest.post(URL, json={**BODY, "agent": agent})
    assert response.status_code == 422


async def test_long_cli_version_422(client_ingest):
    response = await client_ingest.post(URL, json={**BODY, "cli_version": "1" * 65})
    assert response.status_code == 422


async def test_read_listeners_do_not_ingest(settings):
    apps = [create_app(settings, DownPool()), create_service_app(settings, DownPool(), ["t"])]
    for app in apps:
        async with asgi_client(app) as http:
            response = await http.post(URL, json=BODY, headers={"Authorization": "Bearer t"})
        assert response.status_code in (404, 405)


async def test_ingest_app_serves_nothing_else(client_ingest):
    for path in ("/usage/v1/current", "/usage/v1/history", "/usage/v1/sessions"):
        assert (await client_ingest.get(path)).status_code == 404


async def test_body_and_token_never_logged(client_ingest, caplog):
    caplog.set_level(logging.DEBUG)
    await client_ingest.post(URL, json=BODY)
    await client_ingest.post(URL, json={**BODY, "percent": 23.5})
    await client_ingest.post(URL, content=b"23.5 not json", headers={"Content-Type": "x/y"})
    await client_ingest.post(URL, json=BODY, headers={"Authorization": "Bearer wrong-secret"})
    text = "\n".join(r.getMessage() + str(r.exc_text) + str(r.args) for r in caplog.records)
    assert "23.5" not in text
    assert "wrong-secret" not in text
    assert "Bearer i" not in text


async def test_prune_failure_does_not_fail_ingest(client_ingest, fake_samples):
    fake_samples.prune_error = RuntimeError("boom")
    response = await client_ingest.post(URL, json=BODY)
    assert response.status_code == 202
    assert len(fake_samples.pruned) == 1
    assert len(fake_samples.records) == 1


async def test_prune_runs_hourly_after_an_insert(ingest_app, client_ingest, fake_samples):
    clock = {"now": FAKE_NOW}
    ingest_app.state.ingest.now = lambda: clock["now"]
    await client_ingest.post(URL, json=BODY)
    assert fake_samples.pruned == [FAKE_NOW - ingest_app.state.ingest.retention]
    clock["now"] = FAKE_NOW + timedelta(minutes=59)
    await client_ingest.post(URL, json=BODY)
    assert len(fake_samples.pruned) == 1
    clock["now"] = FAKE_NOW + timedelta(minutes=61)
    await client_ingest.post(URL, json=BODY)
    assert len(fake_samples.pruned) == 2


async def test_rejected_post_does_not_prune(client_ingest, fake_samples):
    await client_ingest.post(URL, json={"rate_limits": {}})
    assert fake_samples.pruned == []


def test_runtime_refuses_shared_token(tmp_path, settings_for, capsys):
    service = tmp_path / "service"
    service.write_text("a\nshared\n")
    ingest = tmp_path / "ingest"
    ingest.write_text("shared\n")
    cfg = settings_for(
        "postgresql://u:p@db.invalid/usage",
        service_tokens_file=str(service),
        ingest_tokens_file=str(ingest),
    )
    assert runtime.api_main(cfg) == 78
    assert "USAGE_INGEST_TOKENS_FILE: ingest tokens must differ" in capsys.readouterr().err


def test_runtime_refuses_empty_token_file(tmp_path, settings_for, capsys):
    ingest = tmp_path / "ingest"
    ingest.write_text("\n")
    cfg = settings_for("postgresql://u:p@db.invalid/usage", ingest_tokens_file=str(ingest))
    assert runtime.api_main(cfg) == 78
    assert "USAGE_INGEST_TOKENS_FILE" in capsys.readouterr().err


def test_listener_tokens_distinct_files_ok(tmp_path, settings_for):
    service = tmp_path / "service"
    service.write_text("a\n")
    ingest = tmp_path / "ingest"
    ingest.write_text("b\n")
    cfg = settings_for(
        "postgresql://u:p@db.invalid/usage",
        service_tokens_file=str(service),
        ingest_tokens_file=str(ingest),
    )
    assert runtime._listener_tokens(cfg) == {"service": ["a"], "ingest": ["b"]}
