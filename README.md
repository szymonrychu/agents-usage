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

The prober and the watcher commands are added with their tasks.

## Ingest

The ingest listener (port `USAGE_INGEST_PORT`, only when `USAGE_INGEST_TOKENS_FILE` is set) serves `POST /usage/v1/samples`, `/healthz` and `/readyz` and nothing else, behind the ingest bearer token. It is the one writer of `samples` and `sample_windows`. The agents' token can only add samples; the UI and service listeners refuse the route, and the ingest tokens must differ from the service tokens.

Body (at most 8192 bytes, checked while streaming; unknown keys are refused):

```json
{"agent": "agent-x-0", "cli_version": "2.1.304", "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1794020400}}}
```

- `agent`: optional, `^[A-Za-z0-9._-]{1,128}$`; `cli_version`: optional, at most 64 characters; `rate_limits`: the statusLine object as received.
- `202 {"windows": n}`: n windows were stored. `413 too_large`: the body exceeds the cap. `422 no_windows`: no window in `rate_limits` was usable. `422 invalid_request`: any other bad body. `401`: no or a wrong ingest token.
- The sample time is the server's receive time, never a client clock. `rate_limits` is kept verbatim as `raw`.
- Bodies and tokens are never logged. Samples older than `USAGE_RETENTION_DAYS` are pruned after an insert, at most once an hour.

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
