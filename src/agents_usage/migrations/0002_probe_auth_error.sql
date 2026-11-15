-- forward-only; a probe whose claude run failed on the credential.
ALTER TABLE probes DROP CONSTRAINT IF EXISTS probes_error_check;
ALTER TABLE probes ADD CONSTRAINT probes_error_check
    CHECK (error IN ('timeout', 'cli_exit', 'cli_error', 'no_rate_limit_event', 'auth_error'));
