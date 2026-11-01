import os

import pytest
from agents_common.settings import SettingsError

from agents_usage.settings import Settings, load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("USAGE_"):
            monkeypatch.delenv(key)


def problems(monkeypatch, **env) -> dict[str, str]:
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://x/y")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SettingsError) as exc:
        load_settings()
    return exc.value.problems


def test_defaults(monkeypatch):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://x/y")
    s = load_settings()
    assert isinstance(s, Settings)
    assert (
        s.stale_seconds,
        s.probe_after_seconds,
        s.probe_min_interval_seconds,
        s.retention_days,
    ) == (
        1800,
        1800,
        1800,
        90,
    )
    assert (s.claude_model, s.claude_home, s.http_port, s.service_port, s.ingest_port) == (
        "haiku",
        "/tmp/claude-home",
        8000,
        8001,
        8002,
    )
    assert s.probe_timeout_seconds == 120
    assert s.claude_bin == "claude"
    assert s.claude_token_file is None
    assert s.service_tokens_file is None and s.ingest_tokens_file is None
    assert not hasattr(s, "poll_seconds")
    assert "postgresql://x/y" not in repr(s)


def test_missing_database_url_names_variable(monkeypatch):
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert "USAGE_DATABASE_URL" in exc.value.problems
    assert not any("ORCHESTRATOR" in k for k in exc.value.problems)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("USAGE_PROBE_AFTER_SECONDS", "59"),
        ("USAGE_PROBE_AFTER_SECONDS", "86401"),
        ("USAGE_PROBE_TIMEOUT_SECONDS", "9"),
    ],
)
def test_probe_bounds(monkeypatch, var, value):
    assert var in problems(monkeypatch, **{var: value})


def test_stream_settings_defaults(monkeypatch):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://x/y")
    s = load_settings()
    assert s.orchestrator_url is None and s.orchestrator_token_file is None
    assert s.stream_read_timeout_seconds == 60
    assert s.reconnect_max_seconds == 60
    assert "USAGE_STREAM_READ_TIMEOUT_SECONDS" in problems(
        monkeypatch, USAGE_STREAM_READ_TIMEOUT_SECONDS="5"
    )


def test_bad_cidr_refused(monkeypatch):
    assert "USAGE_ALLOWED_CIDRS" in problems(monkeypatch, USAGE_ALLOWED_CIDRS="nope")


def test_cidr_list_accepted(monkeypatch):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("USAGE_ALLOWED_CIDRS", "10.0.0.0/8,127.0.0.1/32")
    s = load_settings()
    assert s.allowed_cidrs == ["10.0.0.0/8", "127.0.0.1/32"]
    assert [str(n) for n in s.api_networks()] == ["10.0.0.0/8", "127.0.0.1/32"]


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        ({"USAGE_SERVICE_TOKENS_FILE": "/t", "USAGE_SERVICE_PORT": "8000"}, "USAGE_SERVICE_PORT"),
        (
            {
                "USAGE_SERVICE_TOKENS_FILE": "/t",
                "USAGE_INGEST_TOKENS_FILE": "/i",
                "USAGE_INGEST_PORT": "8001",
            },
            "USAGE_INGEST_PORT",
        ),
        ({"USAGE_INGEST_TOKENS_FILE": "/i", "USAGE_INGEST_PORT": "8000"}, "USAGE_INGEST_PORT"),
    ],
)
def test_ports_must_differ(monkeypatch, env, variable):
    assert variable in problems(monkeypatch, **env)


def test_disabled_listener_port_is_not_checked(monkeypatch):
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("USAGE_SERVICE_PORT", "8000")
    assert load_settings().service_port == 8000
