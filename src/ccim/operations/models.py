"""SQLAlchemy mappings for operational-readiness records."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ccim.telemetry.models import Base


class OperationalRunRow(Base):
    __tablename__ = "operational_runs"
    __table_args__ = (
        CheckConstraint(
            "run_category IN "
            "('synthetic-dry-run', 'daily-canary', 'personal-production')",
            name="ck_operational_runs_category",
        ),
        CheckConstraint(
            "status IN "
            "('planned', 'running', 'succeeded', 'failed', 'skipped', 'incomplete')",
            name="ck_operational_runs_status",
        ),
        CheckConstraint(
            "compression_mode IN ('off', 'on')",
            name="ck_operational_runs_compression_mode",
        ),
        CheckConstraint(
            "project_mode IN ('none', 'shared-synthetic', 'private-production')",
            name="ck_operational_runs_project_mode",
        ),
        CheckConstraint(
            "report_label IN ('dry-run', 'dummy', 'actual')",
            name="ck_operational_runs_report_label",
        ),
        CheckConstraint(
            "planned_requests >= 0",
            name="ck_operational_runs_planned_requests",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    utc_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    compression_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    task_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fault_version: Mapped[str] = mapped_column(String(128), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(71))
    project_hash: Mapped[str | None] = mapped_column(String(64))
    project_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    planned_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    report_label: Mapped[str] = mapped_column(String(16), nullable=False)
    actual_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    telemetry_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retention_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)


class OperationalRequestRecordRow(Base):
    __tablename__ = "operational_request_records"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "logical_request_id",
            "attempt",
            name="uq_operational_request_attempt",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'skipped', 'retry', 'incomplete')",
            name="ck_operational_request_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_operational_request_attempt"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("operational_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    logical_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    telemetry_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tokens_input_original_est: Mapped[int | None] = mapped_column(Integer)
    tokens_input_sent_est: Mapped[int | None] = mapped_column(Integer)
    tokens_output_est: Mapped[int | None] = mapped_column(Integer)
    provider_input_tokens: Mapped[int | None] = mapped_column(Integer)
    provider_cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    provider_output_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieve_overhead_tokens_est: Mapped[int | None] = mapped_column(Integer)
    latency_total_ms: Mapped[int | None] = mapped_column(Integer)
    latency_compress_ms: Mapped[int | None] = mapped_column(Integer)
    latency_upstream_ms: Mapped[int | None] = mapped_column(Integer)
    retrieve_cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrieve_persistent_hits: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    retrieve_misses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    guard_blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic_passed: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(128))
    exclusion_reason: Mapped[str | None] = mapped_column(String(128))


class OperationalDailyLedgerRow(Base):
    __tablename__ = "operational_daily_token_ledgers"

    utc_date: Mapped[date] = mapped_column(Date, primary_key=True)
    model_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    known_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_certain: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
