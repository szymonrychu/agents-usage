"""Chart render: api, prober and watcher Deployments, the secrets split and the required values."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-usage"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])
CLAUDE_TOKEN = "".join(["claude", "-", "fixture", "-", "value"])
ORCH_TOKEN = "".join(["orch", "-", "fixture", "-", "value"])
INGEST_TOKEN = "".join(["ingest", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/s"},
    "claude": {"token": CLAUDE_TOKEN},
    "orchestrator": {"url": "http://orch.agents.svc", "token": ORCH_TOKEN},
}
SERVICE = {"serviceListener": {"enabled": True, "tokensSecret": {"name": "usage-service-tokens"}}}
INGEST = {"ingestListener": {"enabled": True, "tokensSecret": {"name": "agents-usage-ingest"}}}
COMPONENTS = ("api", "prober", "watcher")


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "us", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in docs
        if d["kind"] == "Deployment"
    }


def pod(docs: list[dict], component: str) -> dict:
    return deployments(docs)[component]["spec"]["template"]["spec"]


def main(docs: list[dict], component: str) -> dict:
    (container,) = pod(docs, component)["containers"]
    return container


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def _secret_names(spec: dict) -> set[str]:
    return {v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v}


def _secret_refs(spec: dict) -> set[str]:
    """Every Secret a pod mounts or reads through env, init containers included."""
    names = _secret_names(spec)
    for container in [*spec["initContainers"], *spec["containers"]]:
        for e in container.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                names.add(ref["name"])
        for source in container.get("envFrom", []):
            if "secretRef" in source:
                names.add(source["secretRef"]["name"])
    return names


def _mount(container: dict, name: str) -> dict:
    return next(m for m in container["volumeMounts"] if m["name"] == name)


def test_components_render():
    docs = render()
    deps = deployments(docs)
    assert set(deps) == set(COMPONENTS)
    for component in COMPONENTS:
        spec = pod(docs, component)
        container = main(docs, component)
        assert container["command"] == ["usage"]
        assert container["args"] == [component]
        assert container["image"].endswith("/agents-usage:1.2.3")
        (init,) = spec["initContainers"]
        assert init["name"] == "migrate"
        assert spec["automountServiceAccountToken"] is False
    api = main(docs, "api")
    assert api["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8000}
    assert api["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}
    assert [p["name"] for p in api["ports"]] == ["http"]
    for component in ("prober", "watcher"):
        container = main(docs, component)
        assert "ports" not in container
        assert "readinessProbe" not in container
    assert deps["api"]["spec"]["replicas"] == 1


def test_api_has_no_claude_token():
    docs = render(_merge(SERVICE, INGEST))
    spec = pod(docs, "api")
    refs = _secret_refs(spec)
    assert "us-agents-usage-claude" not in refs
    assert "us-agents-usage-orchestrator" not in refs
    assert refs == {
        "us-agents-usage-database",
        "usage-service-tokens",
        "agents-usage-ingest",
    }
    blob = yaml.safe_dump(deployments(docs)["api"])
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in blob
    assert "USAGE_CLAUDE" not in blob
    assert "USAGE_ORCHESTRATOR" not in blob
    assert CLAUDE_TOKEN not in blob and ORCH_TOKEN not in blob


def test_watcher_mounts_orchestrator_token():
    docs = render()
    spec = pod(docs, "watcher")
    container = main(docs, "watcher")
    mount = _mount(container, "orchestrator-token")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == "/var/run/secrets/orchestrator"
    vol = next(v for v in spec["volumes"] if v["name"] == "orchestrator-token")
    assert vol["secret"]["secretName"] == "us-agents-usage-orchestrator"
    assert vol["secret"]["items"] == [{"key": "token", "path": "token"}]
    env = _env(container)
    assert env["USAGE_ORCHESTRATOR_TOKEN_FILE"]["value"] == "/var/run/secrets/orchestrator/token"
    assert env["USAGE_ORCHESTRATOR_URL"]["value"] == "http://orch.agents.svc"
    assert "us-agents-usage-claude" not in _secret_refs(spec)
    assert ORCH_TOKEN not in yaml.safe_dump(deployments(docs)["watcher"])
    secrets = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert secrets["us-agents-usage-orchestrator"]["stringData"] == {"token": ORCH_TOKEN}


def test_watcher_singleton():
    docs = render()
    dep = deployments(docs)["watcher"]
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    assert main(docs, "watcher")["args"] == ["watcher"]
    (init,) = pod(docs, "watcher")["initContainers"]
    assert init["name"] == "migrate" and init["args"] == ["migrate"]
    assert main(docs, "watcher")["securityContext"]["readOnlyRootFilesystem"] is True


def test_prober_has_no_orchestrator_or_ingest_token():
    docs = render(_merge(SERVICE, INGEST))
    spec = pod(docs, "prober")
    assert _secret_refs(spec) == {"us-agents-usage-database", "us-agents-usage-claude"}
    blob = yaml.safe_dump(deployments(docs)["prober"])
    assert "USAGE_ORCHESTRATOR" not in blob
    assert "USAGE_INGEST" not in blob and "USAGE_SERVICE" not in blob
    assert ORCH_TOKEN not in blob and INGEST_TOKEN not in blob


def test_orchestrator_secret_required():
    for overrides, needle in (
        ({"orchestrator": {"token": ""}}, "orchestrator token"),
        ({"orchestrator": {"url": ""}}, "orchestrator.url"),
    ):
        proc = _run(_merge(BASE, overrides))
        assert proc.returncode != 0
        assert needle in proc.stderr


def test_prober_mounts_token_file():
    docs = render()
    spec = pod(docs, "prober")
    container = main(docs, "prober")
    mount = _mount(container, "claude-token")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == "/var/run/secrets/claude"
    vol = next(v for v in spec["volumes"] if v["name"] == "claude-token")
    assert vol["secret"]["secretName"] == "us-agents-usage-claude"
    assert vol["secret"]["items"] == [{"key": "CLAUDE_CODE_OAUTH_TOKEN", "path": "token"}]
    env = _env(container)
    assert env["USAGE_CLAUDE_TOKEN_FILE"]["value"] == "/var/run/secrets/claude/token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert CLAUDE_TOKEN not in yaml.safe_dump(deployments(docs))
    secrets = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert secrets["us-agents-usage-claude"]["stringData"] == {
        "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN
    }


def test_prober_singleton():
    docs = render()
    dep = deployments(docs)["prober"]
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    assert main(docs, "prober")["args"] == ["prober"]


def test_read_only_root_and_tmp():
    docs = render()
    for component in COMPONENTS:
        spec = pod(docs, component)
        container = main(docs, component)
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert _mount(container, "tmp")["mountPath"] == "/tmp"
        assert next(v for v in spec["volumes"] if v["name"] == "tmp") == {
            "name": "tmp",
            "emptyDir": {},
        }
        assert spec["securityContext"]["runAsNonRoot"] is True


def test_migrate_init_on_all_three():
    docs = render()
    for component in COMPONENTS:
        (init,) = pod(docs, component)["initContainers"]
        assert init["name"] == "migrate"
        assert init["command"] == ["usage"]
        assert init["args"] == ["migrate"]
        assert init["securityContext"]["readOnlyRootFilesystem"] is True
        ref = _env(init)["USAGE_DATABASE_URL"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "us-agents-usage-database", "key": "uri"}
        # The init container needs the database only: no token Secret is mounted into it.
        assert {m["name"] for m in init["volumeMounts"]} == {"tmp"}


def test_service_listener_off_by_default():
    docs = render()
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert svc["metadata"]["name"] == "us-agents-usage"
    assert [(p["name"], p["port"]) for p in svc["spec"]["ports"]] == [("http", 80)]
    assert not [e for e in _env(main(docs, "api")) if e.startswith("USAGE_SERVICE")]
    docs = render(SERVICE)
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert {"name": "service", "port": 8001, "targetPort": "service", "protocol": "TCP"} in svc[
        "spec"
    ]["ports"]
    api = main(docs, "api")
    assert {"name": "service", "containerPort": 8001, "protocol": "TCP"} in api["ports"]
    env = _env(api)
    assert env["USAGE_SERVICE_PORT"]["value"] == "8001"
    assert env["USAGE_SERVICE_TOKENS_FILE"]["value"] == "/var/run/secrets/service/tokens"
    mount = _mount(api, "service-tokens")
    assert mount["readOnly"] is True and mount["mountPath"] == "/var/run/secrets/service"
    vol = next(v for v in pod(docs, "api")["volumes"] if v["name"] == "service-tokens")
    assert vol["secret"]["secretName"] == "usage-service-tokens"
    assert vol["secret"]["items"] == [{"key": "tokens", "path": "tokens"}]
    for component in ("prober", "watcher"):
        assert "USAGE_SERVICE_TOKENS_FILE" not in _env(main(docs, component))
        assert "usage-service-tokens" not in _secret_refs(pod(docs, component))


def test_listener_without_secret_fails():
    for key, needle in (
        ("serviceListener", "serviceListener.tokensSecret.name"),
        ("ingestListener", "ingestListener.tokensSecret.name"),
    ):
        proc = _run(_merge(BASE, {key: {"enabled": True}}))
        assert proc.returncode != 0
        assert needle in proc.stderr


def test_ingest_listener():
    docs = render()
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert 8002 not in [p["port"] for p in svc["spec"]["ports"]]
    assert "USAGE_INGEST_TOKENS_FILE" not in _env(main(docs, "api"))
    docs = render(INGEST)
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert {"name": "ingest", "port": 8002, "targetPort": "ingest", "protocol": "TCP"} in svc[
        "spec"
    ]["ports"]
    api = main(docs, "api")
    assert {"name": "ingest", "containerPort": 8002, "protocol": "TCP"} in api["ports"]
    env = _env(api)
    assert env["USAGE_INGEST_PORT"]["value"] == "8002"
    assert env["USAGE_INGEST_TOKENS_FILE"]["value"] == "/var/run/secrets/ingest/tokens"
    assert all("value" not in e or INGEST_TOKEN not in e["value"] for e in env.values())
    mount = _mount(api, "ingest-tokens")
    assert mount["readOnly"] is True and mount["mountPath"] == "/var/run/secrets/ingest"
    vol = next(v for v in pod(docs, "api")["volumes"] if v["name"] == "ingest-tokens")
    assert vol["secret"]["secretName"] == "agents-usage-ingest"
    assert vol["secret"]["items"] == [{"key": "token", "path": "tokens"}]
    for component in ("prober", "watcher"):
        assert "USAGE_INGEST_TOKENS_FILE" not in _env(main(docs, component))
        assert "agents-usage-ingest" not in _secret_refs(pod(docs, component))
    # The chart never renders the ingest token: the Secret is created by agents-helmfile.
    assert not [
        d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "agents-usage-ingest"
    ]


def test_claude_secret_required():
    proc = _run(_merge(BASE, {"claude": {"token": ""}}))
    assert proc.returncode != 0
    assert "claude token" in proc.stderr


def test_existing_secrets_are_referenced_not_rendered():
    docs = render(
        {
            "postgres": {"url": "", "existingSecret": "cnpg-app", "existingSecretKey": "dsn"},
            "claude": {"token": "", "existingSecret": "claude-tok", "existingSecretKey": "t"},
            "orchestrator": {"token": "", "existingSecret": "orch-tok", "existingSecretKey": "o"},
        }
    )
    assert not [d for d in docs if d["kind"] == "Secret"]
    for component in COMPONENTS:
        (init,) = pod(docs, component)["initContainers"]
        assert _env(init)["USAGE_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "cnpg-app",
            "key": "dsn",
        }
    prober = pod(docs, "prober")
    vol = next(v for v in prober["volumes"] if v["name"] == "claude-token")
    assert vol["secret"] == {"secretName": "claude-tok", "items": [{"key": "t", "path": "token"}]}
    watcher = pod(docs, "watcher")
    vol = next(v for v in watcher["volumes"] if v["name"] == "orchestrator-token")
    assert vol["secret"] == {"secretName": "orch-tok", "items": [{"key": "o", "path": "token"}]}


def test_secrets_wired():
    docs = render()
    config = next(d for d in docs if d["kind"] == "ConfigMap")
    blob = yaml.safe_dump(config)
    for secret in (DB_PASS, CLAUDE_TOKEN, ORCH_TOKEN):
        assert secret not in blob
    assert "USAGE_DATABASE_URL" not in config["data"]
    secrets = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert set(secrets) == {
        "us-agents-usage-database",
        "us-agents-usage-claude",
        "us-agents-usage-orchestrator",
    }
    for component in COMPONENTS:
        dep = yaml.safe_dump(deployments(docs)[component])
        for secret in (DB_PASS, CLAUDE_TOKEN, ORCH_TOKEN):
            assert secret not in dep
        for container in [*pod(docs, component)["initContainers"], main(docs, component)]:
            ref = _env(container)["USAGE_DATABASE_URL"]
            assert "value" not in ref
            assert ref["valueFrom"]["secretKeyRef"] == {
                "name": "us-agents-usage-database",
                "key": "uri",
            }


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"postgres": {"url": ""}}, "postgres"),
        ({"postgres": {"url": "x", "existingSecret": "y"}}, "only one of postgres"),
        ({"claude": {"token": "x", "existingSecret": "y"}}, "only one of claude"),
        ({"orchestrator": {"token": "x", "existingSecret": "y"}}, "only one of orchestrator"),
    ],
)
def test_required_values(overrides, needle):
    proc = _run(_merge(BASE, overrides))
    assert proc.returncode != 0
    assert needle in proc.stderr


def test_env_prefix_enforced():
    proc = _run(_merge(BASE, {"env": {"FOO": "1"}}))
    assert proc.returncode != 0
    assert "USAGE_" in proc.stderr


def test_no_poll_setting():
    for overrides in ({}, SERVICE, INGEST):
        proc = _run(_merge(BASE, overrides))
        assert proc.returncode == 0, proc.stderr
        assert "USAGE_POLL" not in proc.stdout


def test_image_tag_defaults_to_app_version():
    docs = render({"image": {"tag": ""}})
    assert main(docs, "api")["image"].endswith("/agents-usage:0.1.0")


def test_env_and_port_reach_the_configmap():
    docs = render({"env": {"USAGE_RETENTION_DAYS": 30}, "components": {"api": {"port": 9000}}})
    config = next(d for d in docs if d["kind"] == "ConfigMap")
    assert config["data"]["USAGE_RETENTION_DAYS"] == "30"
    assert config["data"]["USAGE_HTTP_PORT"] == "9000"
    api = main(docs, "api")
    assert api["readinessProbe"]["httpGet"]["port"] == 9000
    assert api["ports"][0]["containerPort"] == 9000


def test_listener_ports_reach_the_api():
    docs = render(
        {
            "serviceListener": {
                "enabled": True,
                "port": 9001,
                "servicePort": 81,
                "tokensSecret": {"name": "usage-service-tokens"},
            },
            "ingestListener": {
                "enabled": True,
                "port": 9002,
                "servicePort": 82,
                "tokensSecret": {"name": "agents-usage-ingest"},
            },
        }
    )
    env = _env(main(docs, "api"))
    assert env["USAGE_SERVICE_PORT"]["value"] == "9001"
    assert env["USAGE_INGEST_PORT"]["value"] == "9002"
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    by_name = {p["name"]: p for p in svc["spec"]["ports"]}
    assert (by_name["service"]["port"], by_name["service"]["targetPort"]) == (81, "service")
    assert (by_name["ingest"]["port"], by_name["ingest"]["targetPort"]) == (82, "ingest")
    ports = {p["name"]: p["containerPort"] for p in main(docs, "api")["ports"]}
    assert ports == {"http": 8000, "service": 9001, "ingest": 9002}


def test_api_pod_annotations_only_on_api():
    docs = render({"components": {"api": {"podAnnotations": {"x/y": "z"}}}})
    ann = deployments(docs)["api"]["spec"]["template"]["metadata"]["annotations"]
    assert ann["x/y"] == "z"
    for component in ("prober", "watcher"):
        assert (
            "x/y" not in deployments(docs)[component]["spec"]["template"]["metadata"]["annotations"]
        )
