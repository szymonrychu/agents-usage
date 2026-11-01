# agents-usage memory

Lessons that outlive a session. Newest first.

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
