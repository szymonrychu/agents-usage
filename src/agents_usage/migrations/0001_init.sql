CREATE TABLE IF NOT EXISTS samples (
    id bigserial PRIMARY KEY,
    observed_at timestamptz NOT NULL,
    agent text CHECK (agent ~ '^[A-Za-z0-9._-]{1,128}$'),
    cli_version text CHECK (char_length(cli_version) <= 64),
    raw jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_observed_at_idx ON samples (observed_at DESC);
CREATE TABLE IF NOT EXISTS sample_windows (
    sample_id bigint NOT NULL REFERENCES samples (id) ON DELETE CASCADE,
    metric text NOT NULL CHECK (metric ~ '^[a-z0-9_]{1,64}$'),
    percent double precision NOT NULL CHECK (percent >= 0 AND percent <= 1000),
    resets_at timestamptz NOT NULL,
    PRIMARY KEY (sample_id, metric)
);
CREATE INDEX IF NOT EXISTS sample_windows_metric_reset_idx ON sample_windows (metric, resets_at);
CREATE TABLE IF NOT EXISTS probes (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    ok boolean NOT NULL,
    error text CHECK (error IN ('timeout', 'cli_exit', 'cli_error', 'no_rate_limit_event')),
    detail text CHECK (char_length(detail) <= 2000),
    cli_version text,
    cost_usd numeric(12, 6),
    duration_ms integer NOT NULL CHECK (duration_ms >= 0),
    raw jsonb,
    CHECK (ok = (error IS NULL))
);
CREATE INDEX IF NOT EXISTS probes_observed_at_idx ON probes (observed_at DESC);
CREATE INDEX IF NOT EXISTS probes_started_at_idx ON probes (started_at DESC);
CREATE TABLE IF NOT EXISTS probe_windows (
    probe_id bigint NOT NULL REFERENCES probes (id) ON DELETE CASCADE,
    metric text NOT NULL CHECK (metric ~ '^[a-z0-9_]{1,64}$'),
    percent double precision NOT NULL CHECK (percent >= 0 AND percent <= 1000),
    resets_at timestamptz NOT NULL,
    PRIMARY KEY (probe_id, metric)
);
CREATE OR REPLACE VIEW readings AS
    SELECT 'statusline'::text AS source, s.observed_at, s.agent, w.metric, w.percent, w.resets_at
    FROM sample_windows w JOIN samples s ON s.id = w.sample_id
    UNION ALL
    SELECT 'probe'::text, p.observed_at, NULL::text, w.metric, w.percent, w.resets_at
    FROM probe_windows w JOIN probes p ON p.id = w.probe_id
    WHERE p.ok;
CREATE TABLE IF NOT EXISTS session_marks (
    session_id uuid PRIMARY KEY,
    slug text NOT NULL,
    preset_id uuid,
    preset_name text,
    origin text NOT NULL CHECK (origin IN ('ui', 'webhook', 'schedule', 'chat', 'agent', 'other')),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX IF NOT EXISTS session_marks_started_at_idx ON session_marks (started_at);
CREATE INDEX IF NOT EXISTS session_marks_ended_at_idx ON session_marks (ended_at) WHERE ended_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_marks_open_idx ON session_marks (started_at) WHERE ended_at IS NULL;
