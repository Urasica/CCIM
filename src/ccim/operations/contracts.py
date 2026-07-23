"""Stable contracts for operational-readiness data.

The contracts deliberately exclude prompts, source text, credentials, and
absolute paths. Roadmap 02 uses only synthetic or mock-provider records.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from ccim.operations.safety import assert_artifact_safe

SCHEMA_VERSION = "1"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RunCategory(StrEnum):
    SYNTHETIC_DRY_RUN = "synthetic-dry-run"
    DAILY_CANARY = "daily-canary"
    PERSONAL_PRODUCTION = "personal-production"


class RunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INCOMPLETE = "incomplete"


class ObservationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRY = "retry"
    INCOMPLETE = "incomplete"


class CompressionMode(StrEnum):
    OFF = "off"
    ON = "on"


class ProjectMode(StrEnum):
    NONE = "none"
    SHARED_SYNTHETIC = "shared-synthetic"
    PRIVATE_PRODUCTION = "private-production"


class ReportLabel(StrEnum):
    DRY_RUN = "dry-run"
    DUMMY = "dummy"
    ACTUAL = "actual"


class LedgerSource(StrEnum):
    SIMULATED = "simulated"
    LOCAL = "local"
    PROVIDER_DASHBOARD = "provider-dashboard"


@dataclass(frozen=True)
class RetentionPolicy:
    version: str = "retention-v1"
    telemetry_days: int = 90
    evidence_ttl_seconds: int = 3_600
    artifact_days: int = 14
    persistent_evidence_backup_mode: str = "private-encrypted-only"

    def __post_init__(self) -> None:
        _validate_safe_id("retention version", self.version)
        _validate_safe_id(
            "persistent evidence backup mode",
            self.persistent_evidence_backup_mode,
        )
        if min(
            self.telemetry_days,
            self.evidence_ttl_seconds,
            self.artifact_days,
        ) <= 0:
            raise ValueError("retention durations must be positive")

    def telemetry_expiry(self, started_at: datetime) -> datetime:
        utc_iso(started_at)
        return started_at + timedelta(days=self.telemetry_days)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "telemetry_days": self.telemetry_days,
            "evidence_ttl_seconds": self.evidence_ttl_seconds,
            "artifact_days": self.artifact_days,
            "persistent_evidence_backup_mode": self.persistent_evidence_backup_mode,
        }


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def hash_project(project_identifier: str, *, salt: str) -> str:
    """Return a stable one-way project identifier without exposing the source."""
    if not project_identifier or not salt:
        raise ValueError("project identifier and salt are required")
    return hashlib.sha256(f"{salt}\0{project_identifier}".encode()).hexdigest()


def _validate_safe_id(label: str, value: str) -> None:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a path-free stable identifier")


def _validate_nonnegative(label: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{label} must be non-negative")


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    run_category: RunCategory
    status: RunStatus
    session_id: str
    utc_date: date
    started_at: datetime
    commit_sha: str
    config_hash: str
    provider: str
    model: str
    compression_mode: CompressionMode
    task_version: str
    fault_version: str
    project_mode: ProjectMode
    planned_requests: int
    report_label: ReportLabel
    actual_data: bool = False
    ended_at: datetime | None = None
    image_digest: str | None = None
    project_hash: str | None = None
    policy_version: str = "policy-v1"
    telemetry_expires_at: datetime | None = None
    retention_policy_version: str = "retention-v1"

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("session_id", self.session_id),
            ("task_version", self.task_version),
            ("fault_version", self.fault_version),
            ("policy_version", self.policy_version),
            ("retention_policy_version", self.retention_policy_version),
        ):
            _validate_safe_id(label, value)
        if not _COMMIT_RE.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be 7-40 lowercase hexadecimal characters")
        if not _HASH_RE.fullmatch(self.config_hash):
            raise ValueError("config_hash must be a SHA-256 hexadecimal digest")
        if self.image_digest is not None and not _IMAGE_DIGEST_RE.fullmatch(
            self.image_digest
        ):
            raise ValueError("image_digest must be a sha256 digest")
        if self.project_hash is not None and not _HASH_RE.fullmatch(self.project_hash):
            raise ValueError("project_hash must be a SHA-256 hexadecimal digest")
        if self.planned_requests < 0:
            raise ValueError("planned_requests must be non-negative")
        utc_iso(self.started_at)
        utc_iso(self.ended_at)
        utc_iso(self.telemetry_expires_at)
        if self.utc_date != self.started_at.astimezone(UTC).date():
            raise ValueError("utc_date must match started_at at the UTC boundary")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        if self.telemetry_expires_at is None:
            raise ValueError("telemetry_expires_at is required")
        if self.telemetry_expires_at <= self.started_at:
            raise ValueError("telemetry_expires_at must be after started_at")
        expected_project_mode = {
            RunCategory.SYNTHETIC_DRY_RUN: ProjectMode.NONE,
            RunCategory.DAILY_CANARY: ProjectMode.SHARED_SYNTHETIC,
            RunCategory.PERSONAL_PRODUCTION: ProjectMode.PRIVATE_PRODUCTION,
        }[self.run_category]
        if self.project_mode is not expected_project_mode:
            raise ValueError(
                f"{self.run_category.value} requires project_mode="
                f"{expected_project_mode.value}"
            )
        if self.project_mode is ProjectMode.NONE and self.project_hash is not None:
            raise ValueError("synthetic dry-run must not carry a project hash")
        if self.project_mode is not ProjectMode.NONE and self.project_hash is None:
            raise ValueError("canary and production categories require a project hash")
        if self.actual_data and self.run_category is RunCategory.SYNTHETIC_DRY_RUN:
            raise ValueError("synthetic dry-run cannot be marked as actual data")
        if self.actual_data != (self.report_label is ReportLabel.ACTUAL):
            raise ValueError("actual_data and report_label=actual must agree")
        assert_artifact_safe(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_category": self.run_category.value,
            "status": self.status.value,
            "session_id": self.session_id,
            "utc_date": self.utc_date.isoformat(),
            "started_at": utc_iso(self.started_at),
            "ended_at": utc_iso(self.ended_at),
            "commit_sha": self.commit_sha,
            "config_hash": self.config_hash,
            "provider": self.provider,
            "model": self.model,
            "compression_mode": self.compression_mode.value,
            "task_version": self.task_version,
            "fault_version": self.fault_version,
            "image_digest": self.image_digest,
            "project_hash": self.project_hash,
            "project_mode": self.project_mode.value,
            "policy_version": self.policy_version,
            "planned_requests": self.planned_requests,
            "report_label": self.report_label.value,
            "actual_data": self.actual_data,
            "telemetry_expires_at": utc_iso(self.telemetry_expires_at),
            "retention_policy_version": self.retention_policy_version,
        }


@dataclass(frozen=True)
class RequestObservation:
    run_id: str
    logical_request_id: str
    attempt: int
    status: ObservationStatus
    telemetry_complete: bool
    tokens_input_original_est: int | None = None
    tokens_input_sent_est: int | None = None
    tokens_output_est: int | None = None
    provider_input_tokens: int | None = None
    provider_cached_input_tokens: int | None = None
    provider_output_tokens: int | None = None
    retrieve_overhead_tokens_est: int | None = None
    latency_total_ms: int | None = None
    latency_compress_ms: int | None = None
    latency_upstream_ms: int | None = None
    retrieve_cache_hits: int = 0
    retrieve_persistent_hits: int = 0
    retrieve_misses: int = 0
    guard_blocks: int = 0
    semantic_passed: bool | None = None
    error_code: str | None = None
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_safe_id("run_id", self.run_id)
        _validate_safe_id("logical_request_id", self.logical_request_id)
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        for label in (
            "tokens_input_original_est",
            "tokens_input_sent_est",
            "tokens_output_est",
            "provider_input_tokens",
            "provider_cached_input_tokens",
            "provider_output_tokens",
            "retrieve_overhead_tokens_est",
            "latency_total_ms",
            "latency_compress_ms",
            "latency_upstream_ms",
            "retrieve_cache_hits",
            "retrieve_persistent_hits",
            "retrieve_misses",
            "guard_blocks",
        ):
            _validate_nonnegative(label, getattr(self, label))
        if self.error_code is not None:
            _validate_safe_id("error_code", self.error_code)
        if self.exclusion_reason is not None:
            _validate_safe_id("exclusion_reason", self.exclusion_reason)
        if (
            self.status in {ObservationStatus.SKIPPED, ObservationStatus.INCOMPLETE}
            and self.exclusion_reason is None
        ):
            raise ValueError("skipped and incomplete observations require exclusion_reason")
        assert_artifact_safe(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "logical_request_id": self.logical_request_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "telemetry_complete": self.telemetry_complete,
            "tokens_input_original_est": self.tokens_input_original_est,
            "tokens_input_sent_est": self.tokens_input_sent_est,
            "tokens_output_est": self.tokens_output_est,
            "provider_input_tokens": self.provider_input_tokens,
            "provider_cached_input_tokens": self.provider_cached_input_tokens,
            "provider_output_tokens": self.provider_output_tokens,
            "retrieve_overhead_tokens_est": self.retrieve_overhead_tokens_est,
            "latency_total_ms": self.latency_total_ms,
            "latency_compress_ms": self.latency_compress_ms,
            "latency_upstream_ms": self.latency_upstream_ms,
            "retrieve_cache_hits": self.retrieve_cache_hits,
            "retrieve_persistent_hits": self.retrieve_persistent_hits,
            "retrieve_misses": self.retrieve_misses,
            "guard_blocks": self.guard_blocks,
            "semantic_passed": self.semantic_passed,
            "error_code": self.error_code,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True)
class DailyTokenLedger:
    utc_date: date
    model_group: str
    known_tokens: int
    usage_certain: bool
    source: LedgerSource

    def __post_init__(self) -> None:
        _validate_safe_id("model_group", self.model_group)
        _validate_nonnegative("known_tokens", self.known_tokens)
        if self.source is LedgerSource.SIMULATED and self.usage_certain:
            raise ValueError("simulated ledger cannot claim provider usage certainty")
        assert_artifact_safe(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "utc_date": self.utc_date.isoformat(),
            "model_group": self.model_group,
            "known_tokens": self.known_tokens,
            "usage_certain": self.usage_certain,
            "source": self.source.value,
        }
