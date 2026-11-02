# agents-usage

Part of the claude-code-tatara workbench (python).

## What this is

A Python microservice with its own Postgres database that collects the Claude plan usage windows (5-hour, weekly) and serves them to the orchestrator's usage gate and the web UI.

- Primary source: the statusLine `rate_limits` that each agent's claude-code-wrapper forwards, stored as samples. Every percent is on the 0-100 scale.
- Fallback: a lazy probe. The prober runs `claude` only when the service port is read and the newest reading is older than `USAGE_PROBE_AFTER_SECONDS`; there is no poll timer anywhere.
- Session marks: orchestrator session start and end, recorded for the usage chart.

Three listeners, one per audience: the UI port (peer CIDR allowlist behind the SSO proxy), the service port (bearer service tokens, the orchestrator's gate) and the ingest port (one shared ingest token, the agents' samples). Each table has one writer: the api writes `samples` and `sample_windows`, the prober writes `probes` and `probe_windows`, the watcher writes `session_marks`; readers use the view `readings`.

## Development

Run from this directory with mise:

- `mise run test` - unit tests (the Postgres-backed ones need Docker)
- `mise run lint` - ruff check and format check
- `mise run fmt` - ruff format
- `mise run test:chart` - chart render tests
- `mise run lint:chart` - `helm lint` of the chart

## Commands

The `usage` entry point (`uv run usage <command>`):

- `usage migrate` - apply the schema migrations to the service's own database and exit.
- `usage api` - serve the HTTP listeners: the UI port, the service port when `USAGE_SERVICE_TOKENS_FILE` is set and the ingest port when `USAGE_INGEST_TOKENS_FILE` is set. A token file that is empty or unreadable, or a token present in both files, makes the process exit 78.
- `usage prober` - wait for probe requests and run the lazy claude probe when one is due (single instance through the advisory lock `agents-usage-prober`). Exits 78 when `USAGE_CLAUDE_TOKEN_FILE` is unset.

- `usage watcher` - read the orchestrator's session stream and record session start and end marks (single instance through the advisory lock `agents-usage-sessions`). Exits 78 when `USAGE_ORCHESTRATOR_URL` or `USAGE_ORCHESTRATOR_TOKEN_FILE` is unset.

## API

Base path `/usage/v1`, JSON in and out. Three listeners serve it, each with only its own routes:

- the UI port (`USAGE_HTTP_PORT`) serves the read routes to peers inside `USAGE_ALLOWED_CIDRS` (the SSO proxy; forwarding headers are never read);
- the service port (`USAGE_SERVICE_PORT`, when `USAGE_SERVICE_TOKENS_FILE` is set) serves the same read routes behind a bearer service token (the orchestrator's usage gate);
- the ingest port (`USAGE_INGEST_PORT`, when `USAGE_INGEST_TOKENS_FILE` is set) serves only `POST /usage/v1/samples`, behind the agents' ingest token.

`GET /healthz` and `GET /readyz` (`503 not_ready` when the database is unreachable) sit outside the base path on every listener and are exempt from the allowlist and the tokens. The machine-readable contract is `openapi.json`, regenerated with `uv run python -m agents_usage.api.openapi > openapi.json`; a test fails when it drifts from the app or when a route is missing from this section.

Every error has the shape `{"error": {"code": "<code>", "message": "<text>"}}`. Codes: `unauthorized` (401; bearer listeners), `forbidden` (403; UI listener), `not_found` (404), `method_not_allowed` (405), `too_large` (413), `invalid_request` (422; body or query fails validation) and `no_windows` (422; ingest).

Every percent is on the 0-100 scale. The two metrics seen so far are `five_hour` and `seven_day`; clients must ignore metrics they do not know.

### `GET /usage/v1/current`

The usage now, one entry per metric:

```json
{
  "windows": {
    "five_hour": {"percent": 23.5, "resets_at": "2026-11-07T03:00:00Z", "observed_at": "2026-11-07T01:58:12Z", "stale": false, "source": "statusline"},
    "seven_day": {"percent": 22.0, "resets_at": "2026-11-07T11:00:00Z", "observed_at": "2026-11-07T01:58:12Z", "stale": false, "source": "statusline"}
  },
  "last_sample": {"at": "2026-11-07T01:58:12Z", "agent": "agent-x-0"},
  "last_probe": {"at": "2026-11-07T00:10:03Z", "ok": true, "error": null}
}
```

- Per metric the newest window (the latest `resets_at`) is reported. Usage inside a window only grows, so `percent` is the highest reading of that window (readings of the same window are grouped by `resets_at` to the minute) and an idle agent's older re-send never lowers it. `observed_at` and `source` (`statusline` or `probe`) are those of the newest reading of the window.
- A window that has reset (`resets_at` at or before now) is dropped; a metric with no live window is absent. Nothing yet gives `{"windows": {}, "last_sample": null, "last_probe": null}`.
- `stale` is decided here, once: true when the reading is older than `USAGE_STALE_SECONDS`. Callers (the orchestrator's gate) trust it; a stale or absent metric never gates.
- `last_sample` is the newest statusLine sample (`agent` is null when the sender gave none). `last_probe` is the newest probe attempt, failed or not; `error` is `timeout`, `cli_exit`, `cli_error` or `no_rate_limit_event` when `ok` is false, never the stored detail.
- On the service listener only, a call also sends a NOTIFY to the prober when the newest reading is older than `USAGE_PROBE_AFTER_SECONDS` or there is none (at most once a minute per process; the prober decides whether a probe is due). The notification is fire and forget: it never delays or fails the answer. The UI listener never wakes the prober.

### `GET /usage/v1/history`

The readings over a range, oldest first, downsampled:

```
GET /usage/v1/history?from=2026-10-31T00:00:00Z&to=2026-11-07T00:00:00Z&metric=seven_day&max_points=500
```

```json
{
  "from": "2026-10-31T00:00:00Z",
  "to": "2026-11-07T00:00:00Z",
  "bucket_seconds": 1210,
  "points": [
    {"observed_at": "2026-11-06T23:40:11Z", "metric": "seven_day", "percent": 21.5, "resets_at": "2026-11-07T11:00:00Z", "source": "statusline"},
    {"observed_at": "2026-11-07T01:58:12Z", "metric": "seven_day", "percent": 22.0, "resets_at": "2026-11-07T11:00:00Z", "source": "probe"}
  ]
}
```

- Query: `from` and `to` (ISO 8601; a time without a zone is UTC; the range is `from` inclusive to `to` exclusive), default `to` = now and `from` = `to` minus 7 days; `metric` (optional, `^[a-z0-9_]{1,64}$`, default every metric); `max_points` (10..1000, default 500). `from` must be before `to` and the range at most 100 days, else `422 invalid_request`.
- Downsampling: `bucket_seconds = max(1, ceil((to - from) / max_points))`. Per metric and bucket only the peak reading (highest `percent`, ties to the latest) is returned, as the real stored reading, so a range holds at most `max_points` points per metric and a short range comes back unchanged. The server answers with one indexed query over the `readings` view.

### `GET /usage/v1/sessions`

The session start and end marks that overlap a range, for the timeline under the chart:

```
GET /usage/v1/sessions?from=2026-10-31T00:00:00Z&to=2026-11-07T00:00:00Z&limit=1000
```

```json
{
  "from": "2026-10-31T00:00:00Z",
  "to": "2026-11-07T00:00:00Z",
  "truncated": false,
  "marks": [
    {"session_id": "5f0c1c36-8c0e-4a54-9a43-1f3a3b6f2d10", "slug": "fix-flaky-test", "preset_id": "0b6d6d52-1d0e-4a30-8f7e-6a1b0d3c9e11", "preset_name": "reviewer", "origin": "webhook", "started_at": "2026-10-30T22:15:00Z", "ended_at": "2026-11-01T09:40:12Z"},
    {"session_id": "c1a7a9a4-2c35-4c2f-b0a1-5d1f6a8f7b22", "slug": "weekly-report", "preset_id": null, "preset_name": null, "origin": "ui", "started_at": "2026-11-06T23:50:00Z", "ended_at": null}
  ]
}
```

- Query: `from` and `to` as for history (default `to` = now and `from` = `to` minus 7 days; a time without a zone is UTC; `from` must be before `to`, the range at most 100 days, else `422 invalid_request`); `limit` (1..2000, default 1000, else `422 invalid_request`).
- A mark is returned when it overlaps the range: it started before `to` and is still open or ended after `from`. Marks come oldest `started_at` first, so a `limit` cut keeps the oldest and sets `truncated` to true.
- An open session has `ended_at: null`; clients draw it up to now. `origin` is one of `ui`, `webhook`, `schedule`, `chat`, `agent`, `other`; `preset_id` and `preset_name` are null when the session has no preset. A session that ended while the watcher was down has an approximate `ended_at` (the time the watcher noticed).
- Marks come from the orchestrator's session stream (see Watcher); nothing is read from pods.

### `POST /usage/v1/samples`

The agents push their statusLine `rate_limits` here (ingest listener only, behind the ingest bearer token). It is the one writer of `samples` and `sample_windows`. The agents' token can only add samples; the UI and service listeners refuse the route, and the ingest tokens must differ from the service tokens.

Body (at most 8192 bytes, checked while streaming; unknown keys are refused):

```json
{"agent": "agent-x-0", "cli_version": "2.1.304", "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1794020400}, "seven_day": {"used_percentage": 22, "resets_at": 1794049200}}}
```

- `agent`: optional, `^[A-Za-z0-9._-]{1,128}$`; `cli_version`: optional, at most 64 characters; `rate_limits`: the statusLine object as received.
- `202 {"windows": n}`: n windows were stored. `413 too_large`: the body exceeds the cap. `422 no_windows`: no window in `rate_limits` was usable (a window whose `resets_at` is more than 8 days ahead of the receive time or more than 60 seconds before it is dropped and logged, the other windows are kept). `422 invalid_request`: any other bad body. `401`: no or a wrong ingest token.
- The sample time is the server's receive time, never a client clock. `rate_limits` is kept verbatim as `raw`.
- Bodies and tokens are never logged. Samples older than `USAGE_RETENTION_DAYS` are pruned after an insert, at most once an hour.

## Prober

The fallback source when no agent sends statusLine samples. There is no poll timer: the prober LISTENs on the Postgres channel `agents_usage_probe` and does nothing until a notification arrives. Requests are handled one at a time; requests that arrive during a probe are coalesced into one more check. A probe runs only when it is due:

- the newest reading (statusLine sample or successful probe) is older than `USAGE_PROBE_AFTER_SECONDS`, or there is none, and
- the newest probe attempt, failed or not, started more than `USAGE_PROBE_MIN_INTERVAL_SECONDS` ago, or there is none.

One probe is one haiku reply with no tools and no MCP servers:

```
claude -p 'Reply with the single word ok.' --model haiku --output-format stream-json --verbose \
  --tools "" --strict-mcp-config --no-session-persistence --system-prompt 'Reply with the single word ok.'
```

- The child environment is built from scratch: `PATH`, `HOME=$USAGE_CLAUDE_HOME`, `CLAUDE_CODE_OAUTH_TOKEN` and `DISABLE_AUTOUPDATER=1`, nothing else (the database URL never reaches it). Its working directory is `USAGE_CLAUDE_HOME`, created 0700 when missing.
- The token is read from `USAGE_CLAUDE_TOKEN_FILE` on every probe, so a rotated Secret needs no restart. It is never logged, and every stored text (detail, raw event, CLI version) has it replaced by `[redacted]`.
- The child runs in its own process group, killed with SIGKILL after `USAGE_PROBE_TIMEOUT_SECONDS`; stdout is capped at 1 MiB and stderr at 64 KiB (the tails are kept).
- Every attempt is stored in `probes` (windows in `probe_windows`; the prober is their only writer): `ok`, or an error `timeout`, `cli_error` (an error result line, or an empty or unreadable token file), `cli_exit` (a non-zero exit, or the child could not start) or `no_rate_limit_event`, with a redacted detail of at most 2000 characters. A rate limit event makes the probe ok whatever the exit code. Successful probes show up in the `readings` view with source `probe`.
- Probes older than `USAGE_RETENTION_DAYS` are pruned when the prober starts and then once an hour, whether or not anyone asks or a probe runs (the timer only deletes rows; it never runs claude).

## Watcher

Records one `session_marks` row per orchestrator session, for the usage chart. It is its own process so the orchestrator token and the claude OAuth token never share a pod. It reads `GET $USAGE_ORCHESTRATOR_URL/api/v1/stream` (the orchestrator's service port, `Authorization: Bearer <token from USAGE_ORCHESTRATOR_TOKEN_FILE>`, read again on every connection) as server-sent events and uses three events only:

- `snapshot` (first on every connection): the live sessions. A session never seen gets a mark (`started_at` is the session's `created_at`); an open mark whose session is no longer listed is closed with `ended_at` = now, which is approximate when a removal was missed while the watcher was away. The reconciliation is one transaction.
- `session_status`: the session's mark is inserted when it has none.
- `session_removed`: the session's open mark is closed with `ended_at` = now (never before its start). A malformed session id is skipped.

Everything else (`agent_status`, `message_status`, `leader_changed`) is ignored, and of the three events only `id`, `slug`, `preset_id`, `preset_name`, `created_at`, `created_by_service` and `created_by_session` are read. The stream carries message text and prompts; they are dropped when parsed and are never stored or logged. Marks are insert-only: a known session is never inserted again and a closed mark is never reopened. The mark's `origin` comes from the provenance fields: no provenance is `ui`, `agents-webhook` is `webhook`, `agents-schedule` is `schedule`, `agents-matrix-connector` is `chat`, any other service is `other`, and a creating session (an agent) is `agent`.

- A refused (401, 403, any non-2xx), failed, silent (no bytes for `USAGE_STREAM_READ_TIMEOUT_SECONDS`) or ended connection is retried after a jittered exponential backoff, 1 s doubling up to `USAGE_RECONNECT_MAX_SECONDS`; the backoff resets only after a connection stayed up for 30 s. The log has one line per failure (`stream refused status=401`), never the token or a response body.
- Closed marks older than `USAGE_RETENTION_DAYS` are purged when the watcher starts and then once an hour; open marks are never purged.
- The orchestrator's release must list this service's token hash (`tokenHashes`) for the stream route to answer.

## Helm chart

`chart/agents-usage` renders three Deployments from one image: `api` (the listeners), `prober` and `watcher`. The prober and the watcher are singletons (one replica, `Recreate`, no ports). Every pod runs a `migrate` init container, a read-only root filesystem and an `emptyDir` at `/tmp`.

- The Service `<fullname>` has port `http` (80) and, when enabled, `service` (`serviceListener.servicePort`, 8001) and `ingest` (`ingestListener.servicePort`, 8002); the in-cluster URLs are `http://<fullname>:8001` and `http://<fullname>:8002`. The ingress routes the UI port only.
- Each secret reaches one component: the database URL all three; the claude OAuth token (`claude.token` or `claude.existingSecret`, key `CLAUDE_CODE_OAUTH_TOKEN`) the prober only, as the file `USAGE_CLAUDE_TOKEN_FILE`; the orchestrator service token (`orchestrator.token` or `orchestrator.existingSecret`, key `token`) the watcher only, as the file `USAGE_ORCHESTRATOR_TOKEN_FILE`; the service and ingest token files the api only. No token is ever an env value.
- `serviceListener` and `ingestListener` are off by default and need `tokensSecret.name`. The ingest Secret (`agents-usage-ingest`, key `token`) is created by agents-helmfile, never rendered here.
- `env` takes extra `USAGE_*` settings only; there is no poll setting.

## Configuration

Every process reads one validated `USAGE_*` environment. A bad or missing variable makes the process exit 78 at startup with one line per variable. The OAuth token and the database URL are never logged.

| Variable | Default | Meaning |
|---|---|---|
| `USAGE_DATABASE_URL` | required | Postgres DSN of the service's own database |
| `USAGE_HTTP_HOST` | `0.0.0.0` | bind address of every listener |
| `USAGE_HTTP_PORT` | `8000` | UI listener port |
| `USAGE_SERVICE_PORT` | `8001` | service listener port (used when `USAGE_SERVICE_TOKENS_FILE` is set; must differ from the other ports) |
| `USAGE_SERVICE_TOKENS_FILE` | - | file of bearer service tokens, one per line; setting it starts the service listener |
| `USAGE_INGEST_PORT` | `8002` | ingest listener port (used when `USAGE_INGEST_TOKENS_FILE` is set; must differ from the other ports) |
| `USAGE_INGEST_TOKENS_FILE` | - | file of ingest tokens, one per line; setting it starts the ingest listener |
| `USAGE_ALLOWED_CIDRS` | `127.0.0.1/32` | comma-separated CIDRs allowed to reach the UI listener |
| `USAGE_LOG_LEVEL` | `INFO` | log level |
| `USAGE_SHUTDOWN_GRACE_SECONDS` | `20` | graceful shutdown wait |
| `USAGE_STALE_SECONDS` | `1800` | a reading older than this is served as stale (60..86400) |
| `USAGE_PROBE_AFTER_SECONDS` | `1800` | the probe is due when the newest reading is older than this (60..86400) |
| `USAGE_PROBE_MIN_INTERVAL_SECONDS` | `1800` | and the newest probe attempt is older than this (60..86400) |
| `USAGE_PROBE_TIMEOUT_SECONDS` | `120` | the probe's claude run is killed after this (10..600) |
| `USAGE_RETENTION_DAYS` | `90` | samples, probes and closed session marks older than this are pruned (1..3650) |
| `USAGE_ORCHESTRATOR_URL` | - | orchestrator service port the watcher reads the session stream from |
| `USAGE_ORCHESTRATOR_TOKEN_FILE` | - | file holding the orchestrator service token |
| `USAGE_STREAM_READ_TIMEOUT_SECONDS` | `60` | the watcher drops a stream silent for this long (10..600) |
| `USAGE_RECONNECT_MAX_SECONDS` | `60` | upper bound of the watcher's reconnect backoff, which starts at 1 s (5..600) |
| `USAGE_CLAUDE_BIN` | `claude` | the claude executable the prober runs |
| `USAGE_CLAUDE_MODEL` | `haiku` | model of the probe |
| `USAGE_CLAUDE_HOME` | `/tmp/claude-home` | home and working directory of the probe's claude child |
| `USAGE_CLAUDE_TOKEN_FILE` | - | file holding the OAuth token, passed only to the probe's claude child |
