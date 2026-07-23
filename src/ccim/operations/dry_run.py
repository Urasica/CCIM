"""Mock-provider readiness fixture with no network or external model calls."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict

from ccim.operations.budget import evaluate_preflight
from ccim.operations.contracts import (
    SCHEMA_VERSION,
    CompressionMode,
    DailyTokenLedger,
    LedgerSource,
    ObservationStatus,
    ProjectMode,
    ReportLabel,
    RequestObservation,
    RetentionPolicy,
    RunCategory,
    RunMetadata,
    RunStatus,
    hash_project,
)
from ccim.operations.reporting import build_report
from ccim.operations.safety import assert_artifact_safe

_START = datetime(2026, 7, 22, 0, 10, tzinfo=UTC)
_CONFIG_HASH = "a" * 64
_IMAGE_DIGEST = f"sha256:{'b' * 64}"


class _BudgetInputs(TypedDict):
    run_category: RunCategory
    project_mode: ProjectMode
    known_daily_tokens: int
    current_run_tokens: int
    expected_input_tokens: int
    max_output_tokens: int
    usage_certain: bool


class DeterministicMockProvider:
    """Produces fixed usage and failure shapes without importing an HTTP client."""

    network_calls = 0
    external_provider_calls = 0

    def build_observations(self) -> list[RequestObservation]:
        return [
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-001",
                attempt=1,
                status=ObservationStatus.SUCCEEDED,
                telemetry_complete=True,
                tokens_input_original_est=1_000,
                tokens_input_sent_est=1_000,
                tokens_output_est=80,
                provider_input_tokens=1_000,
                provider_output_tokens=80,
                retrieve_overhead_tokens_est=0,
                latency_total_ms=800,
                latency_upstream_ms=760,
                semantic_passed=True,
            ),
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-002",
                attempt=1,
                status=ObservationStatus.FAILED,
                telemetry_complete=True,
                tokens_input_original_est=500,
                tokens_input_sent_est=500,
                provider_input_tokens=500,
                retrieve_overhead_tokens_est=0,
                latency_total_ms=400,
                latency_upstream_ms=380,
                error_code="mock-provider-failure",
            ),
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-003",
                attempt=1,
                status=ObservationStatus.SKIPPED,
                telemetry_complete=True,
                exclusion_reason="budget-preflight-skip",
            ),
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-004",
                attempt=1,
                status=ObservationStatus.RETRY,
                telemetry_complete=True,
                tokens_input_original_est=600,
                tokens_input_sent_est=600,
                provider_input_tokens=600,
                retrieve_overhead_tokens_est=0,
                latency_total_ms=500,
                latency_upstream_ms=480,
                error_code="mock-retryable-error",
            ),
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-004",
                attempt=2,
                status=ObservationStatus.SUCCEEDED,
                telemetry_complete=True,
                tokens_input_original_est=600,
                tokens_input_sent_est=600,
                tokens_output_est=40,
                provider_input_tokens=600,
                provider_output_tokens=40,
                retrieve_overhead_tokens_est=0,
                latency_total_ms=450,
                latency_upstream_ms=420,
                semantic_passed=True,
            ),
            RequestObservation(
                run_id="dry-baseline",
                logical_request_id="req-005",
                attempt=1,
                status=ObservationStatus.INCOMPLETE,
                telemetry_complete=False,
                exclusion_reason="telemetry-record-missing",
            ),
            RequestObservation(
                run_id="dry-compressed",
                logical_request_id="req-001",
                attempt=1,
                status=ObservationStatus.SUCCEEDED,
                telemetry_complete=True,
                tokens_input_original_est=1_000,
                tokens_input_sent_est=600,
                tokens_output_est=90,
                provider_input_tokens=650,
                provider_output_tokens=90,
                retrieve_overhead_tokens_est=50,
                latency_total_ms=920,
                latency_compress_ms=80,
                latency_upstream_ms=800,
                retrieve_cache_hits=1,
                semantic_passed=True,
            ),
            RequestObservation(
                run_id="dry-compressed",
                logical_request_id="req-002",
                attempt=1,
                status=ObservationStatus.SUCCEEDED,
                telemetry_complete=True,
                tokens_input_original_est=800,
                tokens_input_sent_est=500,
                tokens_output_est=70,
                provider_input_tokens=600,
                provider_output_tokens=70,
                retrieve_overhead_tokens_est=100,
                latency_total_ms=880,
                latency_compress_ms=70,
                latency_upstream_ms=770,
                retrieve_persistent_hits=1,
                guard_blocks=1,
                semantic_passed=True,
            ),
            RequestObservation(
                run_id="dummy-canary",
                logical_request_id="req-001",
                attempt=1,
                status=ObservationStatus.SKIPPED,
                telemetry_complete=True,
                exclusion_reason="readiness-only-no-external-call",
            ),
            RequestObservation(
                run_id="dummy-production",
                logical_request_id="req-001",
                attempt=1,
                status=ObservationStatus.SKIPPED,
                telemetry_complete=True,
                exclusion_reason="readiness-only-no-personal-data",
            ),
        ]


def build_runs() -> list[RunMetadata]:
    retention = RetentionPolicy()
    expires = retention.telemetry_expiry(_START)
    project_hash = hash_project("fixture-project", salt="ccim-roadmap-02")
    common: dict[str, Any] = {
        "utc_date": _START.date(),
        "started_at": _START,
        "commit_sha": "0000000",
        "config_hash": _CONFIG_HASH,
        "provider": "mock-local",
        "model": "deterministic-v1",
        "task_version": "task-v1",
        "fault_version": "fault-v1",
        "image_digest": _IMAGE_DIGEST,
        "policy_version": "policy-v1",
        "actual_data": False,
        "ended_at": _START + timedelta(minutes=5),
        "telemetry_expires_at": expires,
        "retention_policy_version": retention.version,
    }
    return [
        RunMetadata(
            **common,
            run_id="dry-baseline",
            run_category=RunCategory.SYNTHETIC_DRY_RUN,
            status=RunStatus.INCOMPLETE,
            session_id="session-baseline",
            compression_mode=CompressionMode.OFF,
            project_mode=ProjectMode.NONE,
            project_hash=None,
            planned_requests=5,
            report_label=ReportLabel.DRY_RUN,
        ),
        RunMetadata(
            **common,
            run_id="dry-compressed",
            run_category=RunCategory.SYNTHETIC_DRY_RUN,
            status=RunStatus.SUCCEEDED,
            session_id="session-compressed",
            compression_mode=CompressionMode.ON,
            project_mode=ProjectMode.NONE,
            project_hash=None,
            planned_requests=2,
            report_label=ReportLabel.DRY_RUN,
        ),
        RunMetadata(
            **common,
            run_id="dummy-canary",
            run_category=RunCategory.DAILY_CANARY,
            status=RunStatus.SKIPPED,
            session_id="session-canary",
            compression_mode=CompressionMode.ON,
            project_mode=ProjectMode.SHARED_SYNTHETIC,
            project_hash=project_hash,
            planned_requests=1,
            report_label=ReportLabel.DUMMY,
        ),
        RunMetadata(
            **common,
            run_id="dummy-production",
            run_category=RunCategory.PERSONAL_PRODUCTION,
            status=RunStatus.SKIPPED,
            session_id="session-production",
            compression_mode=CompressionMode.ON,
            project_mode=ProjectMode.PRIVATE_PRODUCTION,
            project_hash=project_hash,
            planned_requests=1,
            report_label=ReportLabel.DUMMY,
        ),
    ]


def build_budget_matrix() -> list[dict[str, Any]]:
    cases: tuple[tuple[str, _BudgetInputs], ...] = (
        (
            "allowed",
            {
                "run_category": RunCategory.DAILY_CANARY,
                "project_mode": ProjectMode.SHARED_SYNTHETIC,
                "known_daily_tokens": 1_800_000,
                "current_run_tokens": 600_000,
                "expected_input_tokens": 180_000,
                "max_output_tokens": 20_000,
                "usage_certain": True,
            },
        ),
        (
            "daily-hard-stop",
            {
                "run_category": RunCategory.DAILY_CANARY,
                "project_mode": ProjectMode.SHARED_SYNTHETIC,
                "known_daily_tokens": 2_000_001,
                "current_run_tokens": 0,
                "expected_input_tokens": 80_000,
                "max_output_tokens": 20_000,
                "usage_certain": True,
            },
        ),
        (
            "run-hard-cap",
            {
                "run_category": RunCategory.DAILY_CANARY,
                "project_mode": ProjectMode.SHARED_SYNTHETIC,
                "known_daily_tokens": 0,
                "current_run_tokens": 850_000,
                "expected_input_tokens": 40_000,
                "max_output_tokens": 20_000,
                "usage_certain": True,
            },
        ),
        (
            "usage-uncertain",
            {
                "run_category": RunCategory.DAILY_CANARY,
                "project_mode": ProjectMode.SHARED_SYNTHETIC,
                "known_daily_tokens": 0,
                "current_run_tokens": 0,
                "expected_input_tokens": 1_000,
                "max_output_tokens": 1_000,
                "usage_certain": False,
            },
        ),
        (
            "dry-run-external-call",
            {
                "run_category": RunCategory.SYNTHETIC_DRY_RUN,
                "project_mode": ProjectMode.NONE,
                "known_daily_tokens": 0,
                "current_run_tokens": 0,
                "expected_input_tokens": 1_000,
                "max_output_tokens": 1_000,
                "usage_certain": True,
            },
        ),
    )
    return [
        {"case_id": case_id, "decision": evaluate_preflight(**values).as_dict()}
        for case_id, values in cases
    ]


def build_dry_run() -> dict[str, Any]:
    provider = DeterministicMockProvider()
    runs = build_runs()
    observations = provider.build_observations()
    statuses = sorted({item.status.value for item in observations})
    result = {
        "schema_version": SCHEMA_VERSION,
        "report_label": "dry-run",
        "actual_data": False,
        "provider": {
            "kind": "deterministic-mock",
            "network_calls": provider.network_calls,
            "external_provider_calls": provider.external_provider_calls,
        },
        "retention_contract": RetentionPolicy().as_dict(),
        "status_coverage": statuses,
        "simulated_utc_ledger": DailyTokenLedger(
            utc_date=date(2026, 7, 22),
            model_group="gpt-5-mini-family",
            known_tokens=0,
            usage_certain=False,
            source=LedgerSource.SIMULATED,
        ).as_dict(),
        "budget_checks": build_budget_matrix(),
        "reports": {
            "weekly": build_report(
                runs,
                observations,
                window_days=7,
                report_label=ReportLabel.DRY_RUN,
            ),
            "thirty_day": build_report(
                runs,
                observations,
                window_days=30,
                report_label=ReportLabel.DUMMY,
            ),
        },
    }
    assert_artifact_safe(result)
    return result
