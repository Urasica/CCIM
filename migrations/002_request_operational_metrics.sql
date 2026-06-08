-- P5 operational metrics view.
-- Existing deployments still write compact request rows plus feature_flags JSONB.
-- This view exposes frequently compared operational metrics as typed columns
-- without changing the runtime logger path.

CREATE OR REPLACE VIEW request_operational_metrics AS
WITH typed AS (
    SELECT
        *,
        CASE
            WHEN feature_flags->>'compress_history_contexts' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'compress_history_contexts')::integer
            ELSE 0
        END AS ff_compress_history_contexts,
        CASE
            WHEN feature_flags->>'compress_current_turn_contexts' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'compress_current_turn_contexts')::integer
            ELSE 0
        END AS ff_compress_current_turn_contexts,
        CASE
            WHEN feature_flags->>'compress_context_ids' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'compress_context_ids')::integer
            ELSE 0
        END AS ff_compress_context_ids,
        CASE
            WHEN feature_flags->>'retrieve_original_tool_use_tokens_est' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'retrieve_original_tool_use_tokens_est')::integer
            ELSE 0
        END AS ff_retrieve_tool_use_tokens_est,
        CASE
            WHEN feature_flags->>'retrieve_original_result_tokens_est' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'retrieve_original_result_tokens_est')::integer
            ELSE 0
        END AS ff_retrieve_result_tokens_est,
        CASE
            WHEN feature_flags->>'retrieve_original_cache_hits' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'retrieve_original_cache_hits')::integer
            ELSE 0
        END AS ff_retrieve_cache_hits,
        CASE
            WHEN feature_flags->>'retrieve_original_store_fetches' ~ '^-?[0-9]+$'
                THEN (feature_flags->>'retrieve_original_store_fetches')::integer
            ELSE 0
        END AS ff_retrieve_store_fetches,
        LOWER(COALESCE(feature_flags->>'current_turn_write_guard_blocked', 'false'))
            IN ('true', '1', 'yes', 'on') AS ff_guard_blocked,
        LOWER(COALESCE(feature_flags->>'stream_requested', 'false'))
            IN ('true', '1', 'yes', 'on') AS ff_stream_requested
    FROM requests
)
SELECT
    id,
    session_id,
    created_at,
    CASE
        WHEN ff_compress_history_contexts > 0
         AND ff_compress_current_turn_contexts > 0
            THEN 'mixed'
        WHEN ff_compress_current_turn_contexts > 0
            THEN 'current_turn'
        WHEN ff_compress_history_contexts > 0
          OR ff_compress_context_ids > 0
            THEN 'history'
        ELSE 'none'
    END AS compression_mode,
    GREATEST(
        COALESCE(tokens_input_original, 0) - COALESCE(tokens_input_compressed, 0),
        0
    ) AS saved_input_tokens_est,
    ff_retrieve_tool_use_tokens_est AS retrieve_tool_use_tokens_est,
    ff_retrieve_result_tokens_est AS retrieve_result_tokens_est,
    ff_retrieve_cache_hits AS retrieve_cache_hits,
    ff_retrieve_store_fetches AS retrieve_store_fetches,
    feature_flags->>'current_turn_write_guard_mode' AS guard_mode,
    ff_guard_blocked AS guard_blocked,
    feature_flags->>'current_turn_write_guard_block_reason' AS guard_block_reason,
    ff_stream_requested AS stream_requested,
    feature_flags->>'stream_response_mode' AS stream_response_mode,
    feature_flags->>'benchmark_run_id' AS benchmark_run_id,
    feature_flags
FROM typed;
