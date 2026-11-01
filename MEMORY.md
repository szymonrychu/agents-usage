# agents-usage memory

Lessons that outlive a session. Newest first.

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
