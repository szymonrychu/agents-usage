# agents-usage memory

Lessons that outlive a session. Newest first.

## 2026-11-01 - lazy prober (claude-usage-monitor A3)

- Never on a timer (user, mx-99 #624): the prober LISTENs on `agents_usage_probe` and starts claude only after a notification and only when due. Due = the newest reading (any source) is older than `USAGE_PROBE_AFTER_SECONDS` (or none) AND the newest attempt started more than `USAGE_PROBE_MIN_INTERVAL_SECONDS` ago (or none); failed attempts count, so a broken token costs at most one run per interval. No startup probe either.
- Single-flight twice over: the advisory singleton `agents-usage-prober` keeps one prober process, and `run_prober` handles one request at a time, coalescing everything that arrived meanwhile into one more `handle` call. The burst guarantee does not depend on the coalescing: the first attempt's row makes the next check not due.
- argv: `--tools ""` (no tools), `--strict-mcp-config` (no MCP servers from anywhere), `--no-session-persistence` (no session file in HOME), a one-line `--system-prompt` (smallest request that still returns the `rate_limit_event`). No `--bare`: it restricts auth to API keys, and the probe uses the OAuth token. All flags checked against `claude --help` of 2.1.304.
- Env built from scratch (PATH, HOME, CLAUDE_CODE_OAUTH_TOKEN, DISABLE_AUTOUPDATER=1), never `os.environ` plus extras: the database URL must not reach the child. cwd is `USAGE_CLAUDE_HOME` so no project files or hooks are read. stdin is /dev/null.
- The token file is read on every probe (Secret rotation needs no restart); empty or unreadable stores `cli_error` "claude token file unreadable or empty" without running the child. The token is never logged; `redact` covers detail, the raw event (every string, keys too) and the CLI version. stderr is kept from its tail, so `redact` also replaces a token suffix (4+ characters) cut at the start of the text.
- The child gets its own session (process group). SIGKILL to the group on timeout and on cancellation (shutdown), and also after a normal exit so no straggler holding the token survives. Pipe reads get 5 s after the child is gone (a grandchild that escaped the group could hold them), then what was read is used.
- Classification order: timeout, then event present (ok, even with a non-zero exit), then an `is_error` result line (`cli_error`, the result text as detail), then a non-zero exit (`cli_exit`, stderr tail; also when the child could not be started), else `no_rate_limit_event`. A real 2.1.304 run with an invalid token gives `cli_error` "Failed to authenticate. API Error: 401 Invalid bearer token".
- One log line per probe: id, ok with metric percents and cost, or the error code and duration. The detail is stored, not logged.
- The prober writes only `probes`/`probe_windows`; pruning (retention by `observed_at`) runs on start and then every hour on the prober's own loop timer, independent of requests and probes, in its own try/except (2026-11-01: pruning only after a probe starved once statusLine samples made probes not due, so old probes outlived retention). The timer only runs a DELETE, never claude.
- runtime entrypoints are now covered by `tests/test_entrypoints.py` (ported from agents-schedule): help, exit 78, X-Forwarded-For ignored (no database, a `DownPool`), and, with Postgres, migrate, api SIGTERM, the token listeners, prober standby takeover and a notified end-to-end probe through a fake `claude`.

## 2026-11-01 - samples ingest (claude-usage-monitor A8)

- Ingest runs on its own listener and token so the agents' token can only add samples and the read tokens (UI allowlist, service tokens) cannot post; `api_main` loads both token files before opening anything and exits 78 when a token is in both, when a file is empty or unreadable.
- `observed_at` is the server receive time, not a client clock (pod clocks and a replayed body must not move the series); `raw` keeps the posted `rate_limits` verbatim, only `windows_from_statusline` decides what becomes a window.
- Body and bearer are never logged (the token gate logs method and path only); the 8192-byte cap is enforced while streaming, so a chunked body without Content-Length is refused too.
- Prune is piggybacked on ingest (at most hourly, its own try/except, never fails the request): there is no timer in the service. The prune mark is per process, so a restart prunes on the next insert.
- `rate_limits` with NaN/Infinity is refused 422 (jsonb cannot store it) and a `DataError` from the insert (for example a NUL character in a string) maps to 422 instead of a 500.

## 2026-11-01 - usage window parsers (claude-usage-monitor A2)

- Percent is 0-100 everywhere: `windows_from_event` multiplies the probe's 0-1 `utilization` by 100 once; `windows_from_statusline` takes `used_percentage` as is (documented 0-100, a 0.22 stays 0.22: no guessing).
- The statusLine fixture `tests/fixtures/statusline-rate-limits.json` is built from the documented schema (https://code.claude.com/docs/en/statusline); no live TTY capture is possible in an agent pod.
- `unifiedWindows` in the probe's `rate_limit_info` is undocumented (observed on 2.1.304); the documented shape is the top-level `rateLimitType`/`utilization`/`resetsAt`, used as a fallback when `unifiedWindows` is absent. A window without a utilization is no data (empty dict), never a zero.
- `StreamResult.event` is the whole `rate_limit_event` line (spec 3.1 stores the raw object); the last event and the last result line win.
- Entries are skipped, never raised on: bool or non-finite numbers, percent outside 0..1000, `resetsAt` outside the datetime range, metric names outside `^[a-z0-9_]{1,64}$` (the DB CHECK).
- The stream fixtures keep the event, assistant and result lines verbatim; the init line has `cwd`, `original_cwd`, `memory_paths` and `plugins` scrubbed (public repo) and keeps `claude_code_version`.

## 2026-11-01 - service skeleton, settings, migration 0001 (claude-usage-monitor A1)

- Own `Settings` on pydantic-settings, not agents-common `BaseServiceSettings`: that base requires an orchestrator url and token, which the api and the prober never need. `CsvList` and `SettingsError` are reused; `_split_csv` is copied into `Settings` because it lives in the base class, not in `CsvList` (without it a comma list fails).
- Three listeners, three audiences: the UI port (peer CIDR allowlist behind the SSO proxy), the token service port (the orchestrator's usage gate) and the token ingest port (the agents' statusLine samples). Each non-UI listener runs only when its tokens file is set, and every enabled port must differ.
- Claude Code CLI is pinned at 2.1.304 (and node 24.22.0) in the Dockerfile for the prober only; the api never runs it.
- Two reading tables (`samples`, `probes`) plus the view `readings`: one table written by both the api and the prober would break the one-writer rule. `session_marks` belongs to the watcher.
- Percent is 0-100 everywhere (stored, served, compared); the probe's 0-1 `utilization` is multiplied at parse time.
- No poll timer (user, mx-99 #624): the statusLine is the primary source and the probe is a lazy fallback run only when the service port is read and a reading is overdue.
- `session_marks` lives in migration 0001 because nothing was released yet.
- `runtime._run_singleton(settings, lock_name, body)` is the one copy of the advisory-lock worker boilerplate (agents-schedule's `_work`/`_hold_and_run`), reused by the prober and the watcher; `body(settings, pool, stop)` runs while the lock is held.
- Database tests need Docker (testcontainers) and run in CI only; this repo's local runs skip nothing, they error without Docker.
