from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from ccim.migrations import (
    apply_migrations,
    check_database,
    discover_migrations,
    normalize_database_url,
)
from ccim.operations.contracts import ReportLabel
from ccim.operations.dry_run import DeterministicMockProvider, build_runs
from ccim.operations.reporting import build_report

pytestmark = pytest.mark.integration


def _url_for_database(base_url: str, database: str) -> str:
    parsed = urlsplit(normalize_database_url(base_url))
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", "", ""))


@contextmanager
def _temporary_database(base_url: str) -> Iterator[str]:
    name = f"ccim_migration_{uuid.uuid4().hex[:12]}"
    admin_url = _url_for_database(base_url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield _url_for_database(base_url, name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def _test_database_url() -> str:
    value = os.getenv("CCIM_TEST_DATABASE_URL")
    if not value:
        pytest.skip("CCIM_TEST_DATABASE_URL is not configured")
    return value


def test_migration_apply_is_idempotent_on_new_database() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        first = apply_migrations(database_url, migrations)
        second = apply_migrations(database_url, migrations)
        checked = check_database(database_url, migrations)

    assert first.current is True
    assert second.current is True
    assert checked.current is True
    assert checked.applied_versions == checked.expected_versions


def test_migration_apply_adopts_existing_idempotent_schema() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        with psycopg.connect(normalize_database_url(database_url)) as connection:
            for migration in migrations:
                connection.execute(migration.sql)

        before = check_database(database_url, migrations)
        after = apply_migrations(database_url, migrations)

    assert before.current is False
    assert before.missing_versions == tuple(item.version for item in migrations)
    assert after.current is True


def test_operational_readiness_schema_and_view_are_queryable() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        apply_migrations(database_url, migrations)
        with psycopg.connect(normalize_database_url(database_url)) as connection:
            relations = connection.execute(
                """
                SELECT to_regclass('public.operational_runs'),
                       to_regclass('public.operational_request_records'),
                       to_regclass('public.operational_daily_token_ledgers'),
                       to_regclass('public.operational_run_metrics')
                """
            ).fetchone()
            metric_columns = connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'operational_run_metrics'
                ORDER BY ordinal_position
                """
            ).fetchall()

    assert all(item is not None for item in relations)
    assert "telemetry_completeness_pct" in {row[0] for row in metric_columns}
    assert "net_saved_tokens_est" in {row[0] for row in metric_columns}


def test_operational_fixture_matches_postgres_metrics_view() -> None:
    base_url = _test_database_url()
    migrations = discover_migrations()
    with _temporary_database(base_url) as database_url:
        apply_migrations(database_url, migrations)
        run = next(item for item in build_runs() if item.run_id == "dry-compressed")
        observations = [
            item
            for item in DeterministicMockProvider().build_observations()
            if item.run_id == run.run_id
        ]
        expected = build_report(
            [run],
            observations,
            window_days=7,
            report_label=ReportLabel.DRY_RUN,
        )["categories"][0]["runs"][0]
        with psycopg.connect(normalize_database_url(database_url)) as connection:
            connection.execute(
                """
                INSERT INTO operational_runs (
                    run_id, run_category, status, session_id, utc_date,
                    started_at, ended_at, commit_sha, config_hash, provider,
                    model, compression_mode, task_version, fault_version,
                    image_digest, project_hash, project_mode, policy_version,
                    planned_requests, report_label, actual_data,
                    telemetry_expires_at, retention_policy_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    run.run_id,
                    run.run_category.value,
                    run.status.value,
                    run.session_id,
                    run.utc_date,
                    run.started_at,
                    run.ended_at,
                    run.commit_sha,
                    run.config_hash,
                    run.provider,
                    run.model,
                    run.compression_mode.value,
                    run.task_version,
                    run.fault_version,
                    run.image_digest,
                    run.project_hash,
                    run.project_mode.value,
                    run.policy_version,
                    run.planned_requests,
                    run.report_label.value,
                    run.actual_data,
                    run.telemetry_expires_at,
                    run.retention_policy_version,
                ),
            )
            for observation in observations:
                connection.execute(
                    """
                    INSERT INTO operational_request_records (
                        run_id, logical_request_id, attempt, status,
                        telemetry_complete, tokens_input_original_est,
                        tokens_input_sent_est, tokens_output_est,
                        provider_input_tokens, provider_cached_input_tokens,
                        provider_output_tokens, retrieve_overhead_tokens_est,
                        latency_total_ms, latency_compress_ms,
                        latency_upstream_ms, retrieve_cache_hits,
                        retrieve_persistent_hits, retrieve_misses, guard_blocks,
                        semantic_passed, error_code, exclusion_reason
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        observation.run_id,
                        observation.logical_request_id,
                        observation.attempt,
                        observation.status.value,
                        observation.telemetry_complete,
                        observation.tokens_input_original_est,
                        observation.tokens_input_sent_est,
                        observation.tokens_output_est,
                        observation.provider_input_tokens,
                        observation.provider_cached_input_tokens,
                        observation.provider_output_tokens,
                        observation.retrieve_overhead_tokens_est,
                        observation.latency_total_ms,
                        observation.latency_compress_ms,
                        observation.latency_upstream_ms,
                        observation.retrieve_cache_hits,
                        observation.retrieve_persistent_hits,
                        observation.retrieve_misses,
                        observation.guard_blocks,
                        observation.semantic_passed,
                        observation.error_code,
                        observation.exclusion_reason,
                    ),
                )
            metrics = connection.execute(
                """
                SELECT telemetry_completeness_pct,
                       gross_saved_tokens_est,
                       retrieve_overhead_tokens_est,
                       net_saved_tokens_est
                FROM operational_run_metrics
                WHERE run_id = %s
                """,
                (run.run_id,),
            ).fetchone()

    assert float(metrics[0]) == expected["telemetry_completeness_pct"]
    assert metrics[1] == expected["gross_saved_tokens_est"]
    assert metrics[2] == expected["retrieve_overhead_tokens_est"]
    assert metrics[3] == expected["net_saved_tokens_est"]
