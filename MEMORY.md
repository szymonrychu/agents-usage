# agents-usage memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - plausibility bound on a window's resets_at

- One bad or forged sample with a far-future `resets_at` became the current window for days (`current_view` keeps the newest reset per metric). `parser._windows` now drops a window whose reset is more than `MAX_RESET_AHEAD` (8 days) after the sample's observed time, or more than `RESET_PAST_SKEW` (60 s) before it, and logs one warning per sample with the count, metrics and resets. The rest of the sample is kept; a sample left with no window is the existing 422 `no_windows` (ingest) or an ok probe with no windows.
- Both sources pass the same function: `windows_from_statusline` (observed = the server receive time, never a client clock) and `windows_from_event`/`parse_stream` (observed = the probe's `observed_at`). `observed_at` is a required argument so no caller can skip the bound. The `raw` column still keeps the posted body verbatim.
- Ahead bound 8 days: the longest window is 7 days and `db.readings.WINDOW_LOOKBACK` is already 8 days, so a reset the lookback could not even see is never plausible. Boundaries are inclusive (exactly 8 days ahead and exactly 60 s behind are kept; "more than" is dropped).
- Past skew 60 s, from the code's own conventions: windows are grouped by `date_trunc('minute', resets_at)` and the other short intervals are 60 s (`ProbeTrigger` min gap, the lowest `stale_seconds`), so a minute is the precision the service already works at; a statusLine posted as a window rolled over, or a pod clock a few seconds off, stays accepted. Anything older is a window that already ended (`current_view` drops it anyway).
- Test clocks: the entrypoint tests and `fake_claude.py` (fixture mode) run on the real clock, so they now use a reset relative to now; fixed 2026-11-07 resets would have turned into past windows and failed after that date.

## 2026-11-01 - helm chart (claude-usage-monitor A5)

- Three Deployments (api, prober, watcher) from one image, so each token reaches exactly one pod type: the claude OAuth token only the prober, the orchestrator service token only the watcher, the service and ingest token files only the api. All are mounted as files (Secret key projected to `token`/`tokens`) and the settings carry only the file paths; the migrate init container mounts nothing but `/tmp`.
- The prober and the watcher hold an advisory lock, so they are fixed at one replica with `Recreate` (a rolling update would start the new pod while the old one still holds the lock). Only the api has `replicas` and `podAnnotations` values.
- The Service is named `<fullname>` (not `-api` as in agents-schedule): agents-helmfile and the orchestrator use `http://agents-usage:8001` and `:8002`. Ports are named `http`, `service`, `ingest`; the ingress backend is `http` only so the token listeners are never routed outside the cluster.
- Component-only settings (listener ports and token paths, claude and orchestrator settings) are container env entries, not ConfigMap keys, because the ConfigMap is `envFrom` of every pod.
- The ingest Secret is not rendered by the chart: agents-helmfile owns it (key `token`, no newline) and shares it with the agent pods as `CCW_USAGE_TOKEN`. Values were renamed from agents-schedule (`orchestrator.tokenSecret` -> `existingSecret`/`existingSecretKey`, `admin` -> `serviceListener`).
- `.mise.toml` `lint:chart` passes `orchestrator.url`, `orchestrator.token` and `claude.existingSecret` because the render fails without them (preflight S04); the CI chart job runs the mise tasks, so it needed no edit.
- The prober memory limit is 1Gi: claude is a node process that runs as a child.

## 2026-11-01 - session marks watcher (claude-usage-monitor A6)

- The watcher is its own component (a third command, lock `agents-usage-sessions`) so the claude OAuth token (prober) and the orchestrator service token (watcher) never share a pod. It is also the only writer of `session_marks`.
- Marks are insert-only (`ON CONFLICT DO NOTHING`): a known session is never inserted again, a closed mark is never reopened. `ended_at` is `now()` when the removal event arrives or when a snapshot lacks the session, so it is approximate after a missed removal; `close_mark` uses `greatest(now, started_at)` because the table CHECK forbids an end before the start and the orchestrator's `created_at` clock may be ahead of ours.
- Only the start and end of sessions are marked (Q9): no pods, no scale events, no agent status.
- Purge deletes closed marks only (`ended_at < now - USAGE_RETENTION_DAYS`), at start and hourly in its own loop and try/except; an open mark of any age stays.
- The stream carries message text and prompts (`message_status`, the session `spec`). `mark_from_session` reads seven fields and builds a frozen `Mark`, so everything else is dropped at parse; `agent_status`, `message_status` and `leader_changed` are not even JSON-decoded. A test feeds a fixture with message bodies and asserts they are in no log line and no repository call.
- A `snapshot` whose `sessions` is not a list is ignored, not read as empty (an empty list closes every open mark). A snapshot entry with a malformed time or slug is skipped with a warning (no mark inserted), but its id still counts as live (`marks.live_ids`), so a snapshot never closes the open mark of a session that is listed (fix round 1, 2026-11-01); only entries without a valid id are ignored entirely.
- Backoff is `min(max, 2**min(attempt, 30)) * (0.5 + jitter/2)` (exponent capped so attempt 1024+ cannot overflow); every end of a connection (error, refusal, EOF) sleeps one step, and the counter resets to 0 only when the connection stayed up at least 30 s (`STABLE_AFTER_SECONDS`, injectable), not at each snapshot: a flapping orchestrator that snapshots and drops would otherwise be re-polled about once a second (fix round 1, 2026-11-01). A 401 loop settles at `USAGE_RECONNECT_MAX_SECONDS`.
- Tests inject a `MarksRepo` stand-in (reconcile/insert/close/purge), the clock, the sleep, the jitter and an httpx `MockTransport`; the pure parts (`sse.py`, `marks.py`) and `watcher.py` need no database. The SSE parser is a line-fed class (`SseParser`) because the watcher reads lines asynchronously; `parse_sse` wraps it. Unlike the SSE spec, the `id` does not persist to the next event (nothing here uses Last-Event-ID).

## 2026-11-01 - current and history API (claude-usage-monitor A4)

- `current` = the highest reading of the newest window per metric: usage inside a window only grows, so an idle agent's older re-send never lowers it. Readings are grouped by `resets_at` truncated to the minute (resets jitter by seconds between senders); the group with the latest `resets_at` wins and its `observed_at`/`source` are those of its newest reading. A window with `resets_at <= now` is dropped (the orchestrator pins the same boundary).
- Staleness is decided here once (`now - observed_at > USAGE_STALE_SECONDS`); the orchestrator trusts `stale` and never recomputes it.
- Only the service listener's `current` wakes the prober, by NOTIFY, as a fire-and-forget task, at most once a minute per process: the gate is "something asks" (the orchestrator's usage gate), and a person looking at the web UI Usage page is not. The UI app has no trigger (`app.state.probe_trigger` is None), so the page never costs a probe and its `current` does not even read the newest time. A failed NOTIFY is logged and swallowed; the mark is set before the await so concurrent requests send one.
- History keeps the peak reading per metric and bucket (highest percent, ties to the latest) as the real stored row, not an average: the chart must not hide a spike. `bucket_seconds = max(1, ceil(range / max_points))`, so a range never exceeds `max_points` points per metric and a short range is returned unchanged. The range is `[from, to)`, at most 100 days (retention is 90), default last 7 days.
- `newest_at` reads the newest `observed_at` of `samples` and of ok `probes` that have a `probe_windows` row (two index probes, the probe one as `ORDER BY observed_at DESC LIMIT 1` with an EXISTS, which stays on the index where a `max()` with EXISTS falls back to a Seq Scan) instead of the `readings` view, whose UNION ALL over joins cannot answer a max from an index. Equivalent to the view because ingest refuses a sample without windows; an ok probe without windows (the parser can leave none) is not a reading in the view and so not here either (fixed 2026-11-01: it used to count, and the probe request check disagreed with the prober's due check). The EXPLAIN test asserts no Seq Scan on `samples` or `probes` for the 90-day history and for `current`'s queries; the planner may still hash join and scan the narrow `sample_windows`/`probe_windows` tables, which is not asserted.
- Every served instant is converted to UTC in the response models, whatever the database session time zone.
- `openapi.json` is one document for all three listeners (the ingest route is documented by hand because the route reads the raw request to cap the body while streaming); a test pins routes per listener, the document and the README.

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

## 2026-11-01 - sessions API (claude-usage-monitor A7)

- Overlap predicate: a mark belongs to `[from, to)` when `started_at < to AND (ended_at IS NULL OR ended_at > from)`. A mark that ended exactly at `from` is out, one that started exactly at `to` is out (half-open, as history).
- Open marks are served with `ended_at: null` (never filled with now): the server cannot know the session still runs after the mark was read, and the UI runs an open mark to the right edge of its chart. The key is always present (the web client types it as `string | null`).
- Order is `started_at, session_id` (the second key only makes ties stable); `limit` (1..2000, default 1000) keeps the oldest marks and `truncated` says more existed: the query fetches `limit + 1` rows and the route trims, so there is no second count query.
- The statement uses `session_marks_started_at_idx` and the partial open/ended indexes; no EXPLAIN test (the table is small: one row per session, 90 days of retention).
- The route is mounted on the UI and the service listeners like `current` and `history`; it does not wake the prober.

## 2026-11-11 - current reports the newest reading's percent (T-122)

- `WINDOW_GROUPS_SQL` took `max(percent)` per window on the belief that usage only grows inside a window. Anthropic issued an early bonus reset that lowered seven_day to 34% without changing `resets_at`, so `/current` kept reporting 92%. Percent is now the newest reading's (`array_agg(percent ORDER BY observed_at DESC)[1]`), same row as `observed_at` and `source`. `HISTORY_SQL` keeps its per-bucket peak downsampling.
- Trade-off: an idle agent's stale re-send of an older, higher value inside the newest reading's window can no longer be masked by max; the newest observed reading wins.
