-- Roadmap 02: operational run identity, request observations, and UTC budget ledger.
-- The schema stores only structured metrics and hashed project identity. Prompts,
-- source text, credentials, and absolute paths have no columns here.

CREATE TABLE IF NOT EXISTS operational_runs (
    run_id                      TEXT PRIMARY KEY,
    run_category                TEXT NOT NULL,
    status                      TEXT NOT NULL,
    session_id                  TEXT NOT NULL,
    utc_date                    DATE NOT NULL,
    started_at                  TIMESTAMPTZ NOT NULL,
    ended_at                    TIMESTAMPTZ,
    commit_sha                  TEXT NOT NULL,
    config_hash                 TEXT NOT NULL,
    provider                    TEXT NOT NULL,
    model                       TEXT NOT NULL,
    compression_mode            TEXT NOT NULL,
    task_version                TEXT NOT NULL,
    fault_version               TEXT NOT NULL,
    image_digest                TEXT,
    project_hash                TEXT,
    project_mode                TEXT NOT NULL,
    policy_version              TEXT NOT NULL,
    planned_requests            INTEGER NOT NULL,
    report_label                TEXT NOT NULL,
    actual_data                 BOOLEAN NOT NULL DEFAULT FALSE,
    telemetry_expires_at        TIMESTAMPTZ NOT NULL,
    retention_policy_version    TEXT NOT NULL,

    CONSTRAINT ck_operational_runs_category
        CHECK (run_category IN (
            'synthetic-dry-run', 'daily-canary', 'personal-production'
        )),
    CONSTRAINT ck_operational_runs_status
        CHECK (status IN (
            'planned', 'running', 'succeeded', 'failed', 'skipped', 'incomplete'
        )),
    CONSTRAINT ck_operational_runs_compression_mode
        CHECK (compression_mode IN ('off', 'on')),
    CONSTRAINT ck_operational_runs_project_mode
        CHECK (project_mode IN (
            'none', 'shared-synthetic', 'private-production'
        )),
    CONSTRAINT ck_operational_runs_category_project
        CHECK (
            (run_category = 'synthetic-dry-run'
                AND project_mode = 'none'
                AND project_hash IS NULL)
            OR
            (run_category = 'daily-canary'
                AND project_mode = 'shared-synthetic'
                AND project_hash IS NOT NULL)
            OR
            (run_category = 'personal-production'
                AND project_mode = 'private-production'
                AND project_hash IS NOT NULL)
        ),
    CONSTRAINT ck_operational_runs_report_label
        CHECK (report_label IN ('dry-run', 'dummy', 'actual')),
    CONSTRAINT ck_operational_runs_actual_data
        CHECK (
            (actual_data AND report_label = 'actual'
                AND run_category <> 'synthetic-dry-run')
            OR
            (NOT actual_data AND report_label <> 'actual')
        ),
    CONSTRAINT ck_operational_runs_planned_requests
        CHECK (planned_requests >= 0),
    CONSTRAINT ck_operational_runs_time_order
        CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT ck_operational_runs_retention
        CHECK (
            telemetry_expires_at IS NULL
            OR telemetry_expires_at > started_at
        )
);

CREATE INDEX IF NOT EXISTS idx_operational_runs_category_date
    ON operational_runs (run_category, utc_date DESC);

CREATE INDEX IF NOT EXISTS idx_operational_runs_cohort
    ON operational_runs (
        commit_sha,
        config_hash,
        policy_version,
        provider,
        model
    );

CREATE TABLE IF NOT EXISTS operational_request_records (
    id                              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id                          TEXT NOT NULL
        REFERENCES operational_runs(run_id) ON DELETE CASCADE,
    logical_request_id              TEXT NOT NULL,
    attempt                         INTEGER NOT NULL,
    status                          TEXT NOT NULL,
    telemetry_complete              BOOLEAN NOT NULL,
    tokens_input_original_est       INTEGER,
    tokens_input_sent_est           INTEGER,
    tokens_output_est               INTEGER,
    provider_input_tokens           INTEGER,
    provider_cached_input_tokens    INTEGER,
    provider_output_tokens          INTEGER,
    retrieve_overhead_tokens_est    INTEGER,
    latency_total_ms                INTEGER,
    latency_compress_ms             INTEGER,
    latency_upstream_ms             INTEGER,
    retrieve_cache_hits             INTEGER NOT NULL DEFAULT 0,
    retrieve_persistent_hits        INTEGER NOT NULL DEFAULT 0,
    retrieve_misses                 INTEGER NOT NULL DEFAULT 0,
    guard_blocks                    INTEGER NOT NULL DEFAULT 0,
    semantic_passed                 BOOLEAN,
    error_code                      TEXT,
    exclusion_reason                TEXT,

    CONSTRAINT uq_operational_request_attempt
        UNIQUE (run_id, logical_request_id, attempt),
    CONSTRAINT ck_operational_request_attempt
        CHECK (attempt >= 1),
    CONSTRAINT ck_operational_request_status
        CHECK (status IN (
            'succeeded', 'failed', 'skipped', 'retry', 'incomplete'
        )),
    CONSTRAINT ck_operational_request_exclusion
        CHECK (
            status NOT IN ('skipped', 'incomplete')
            OR exclusion_reason IS NOT NULL
        ),
    CONSTRAINT ck_operational_request_nonnegative
        CHECK (
            COALESCE(tokens_input_original_est, 0) >= 0
            AND COALESCE(tokens_input_sent_est, 0) >= 0
            AND COALESCE(tokens_output_est, 0) >= 0
            AND COALESCE(provider_input_tokens, 0) >= 0
            AND COALESCE(provider_cached_input_tokens, 0) >= 0
            AND COALESCE(provider_output_tokens, 0) >= 0
            AND COALESCE(retrieve_overhead_tokens_est, 0) >= 0
            AND COALESCE(latency_total_ms, 0) >= 0
            AND COALESCE(latency_compress_ms, 0) >= 0
            AND COALESCE(latency_upstream_ms, 0) >= 0
            AND retrieve_cache_hits >= 0
            AND retrieve_persistent_hits >= 0
            AND retrieve_misses >= 0
            AND guard_blocks >= 0
        )
);

CREATE INDEX IF NOT EXISTS idx_operational_request_records_run
    ON operational_request_records (run_id, logical_request_id, attempt);

CREATE TABLE IF NOT EXISTS operational_daily_token_ledgers (
    utc_date        DATE NOT NULL,
    model_group     TEXT NOT NULL,
    known_tokens    INTEGER NOT NULL,
    usage_certain   BOOLEAN NOT NULL,
    source          TEXT NOT NULL,

    PRIMARY KEY (utc_date, model_group),
    CONSTRAINT ck_operational_daily_tokens
        CHECK (known_tokens >= 0),
    CONSTRAINT ck_operational_daily_source
        CHECK (source IN ('simulated', 'local', 'provider-dashboard')),
    CONSTRAINT ck_operational_simulated_uncertain
        CHECK (source <> 'simulated' OR NOT usage_certain)
);

CREATE OR REPLACE VIEW operational_run_metrics AS
SELECT
    run.run_id,
    run.run_category,
    run.utc_date,
    run.commit_sha,
    run.config_hash,
    run.provider,
    run.model,
    run.compression_mode,
    run.policy_version,
    run.report_label,
    run.actual_data,
    run.planned_requests,
    COUNT(record.id) AS attempt_records,
    COUNT(DISTINCT record.logical_request_id) AS observed_requests,
    COUNT(DISTINCT record.logical_request_id) FILTER (
        WHERE record.telemetry_complete
          AND record.status IN ('succeeded', 'failed', 'skipped')
    ) AS telemetry_complete_requests,
    CASE
        WHEN run.planned_requests = 0 THEN NULL
        ELSE ROUND(
            100.0
            * COUNT(DISTINCT record.logical_request_id) FILTER (
                WHERE record.telemetry_complete
                  AND record.status IN ('succeeded', 'failed', 'skipped')
            )
            / run.planned_requests,
            2
        )
    END AS telemetry_completeness_pct,
    COUNT(record.id) FILTER (
        WHERE record.telemetry_complete
          AND record.tokens_input_original_est IS NOT NULL
          AND record.tokens_input_sent_est IS NOT NULL
    ) AS metric_sample_count,
    SUM(
        GREATEST(
            record.tokens_input_original_est - record.tokens_input_sent_est,
            0
        )
    ) FILTER (
        WHERE record.telemetry_complete
          AND record.tokens_input_original_est IS NOT NULL
          AND record.tokens_input_sent_est IS NOT NULL
    ) AS gross_saved_tokens_est,
    SUM(COALESCE(record.retrieve_overhead_tokens_est, 0)) FILTER (
        WHERE record.telemetry_complete
          AND record.tokens_input_original_est IS NOT NULL
          AND record.tokens_input_sent_est IS NOT NULL
    ) AS retrieve_overhead_tokens_est,
    SUM(
        GREATEST(
            record.tokens_input_original_est - record.tokens_input_sent_est,
            0
        )
        - COALESCE(record.retrieve_overhead_tokens_est, 0)
    ) FILTER (
        WHERE record.telemetry_complete
          AND record.tokens_input_original_est IS NOT NULL
          AND record.tokens_input_sent_est IS NOT NULL
    ) AS net_saved_tokens_est
FROM operational_runs AS run
LEFT JOIN operational_request_records AS record
    ON record.run_id = run.run_id
GROUP BY run.run_id;
