"""Persistence adapter for operational-readiness contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ccim.operations.contracts import (
    CompressionMode,
    DailyTokenLedger,
    LedgerSource,
    ObservationStatus,
    ProjectMode,
    ReportLabel,
    RequestObservation,
    RunCategory,
    RunMetadata,
    RunStatus,
)
from ccim.operations.models import (
    OperationalDailyLedgerRow,
    OperationalRequestRecordRow,
    OperationalRunRow,
)


class OperationalRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def create_run(self, run: RunMetadata) -> None:
        async with self._session() as session, session.begin():
            session.add(
                OperationalRunRow(
                    run_id=run.run_id,
                    run_category=run.run_category.value,
                    status=run.status.value,
                    session_id=run.session_id,
                    utc_date=run.utc_date,
                    started_at=run.started_at,
                    ended_at=run.ended_at,
                    commit_sha=run.commit_sha,
                    config_hash=run.config_hash,
                    provider=run.provider,
                    model=run.model,
                    compression_mode=run.compression_mode.value,
                    task_version=run.task_version,
                    fault_version=run.fault_version,
                    image_digest=run.image_digest,
                    project_hash=run.project_hash,
                    project_mode=run.project_mode.value,
                    policy_version=run.policy_version,
                    planned_requests=run.planned_requests,
                    report_label=run.report_label.value,
                    actual_data=run.actual_data,
                    telemetry_expires_at=run.telemetry_expires_at,
                    retention_policy_version=run.retention_policy_version,
                )
            )

    async def record_observation(self, observation: RequestObservation) -> None:
        async with self._session() as session, session.begin():
            session.add(
                OperationalRequestRecordRow(
                    run_id=observation.run_id,
                    logical_request_id=observation.logical_request_id,
                    attempt=observation.attempt,
                    status=observation.status.value,
                    telemetry_complete=observation.telemetry_complete,
                    tokens_input_original_est=observation.tokens_input_original_est,
                    tokens_input_sent_est=observation.tokens_input_sent_est,
                    tokens_output_est=observation.tokens_output_est,
                    provider_input_tokens=observation.provider_input_tokens,
                    provider_cached_input_tokens=(
                        observation.provider_cached_input_tokens
                    ),
                    provider_output_tokens=observation.provider_output_tokens,
                    retrieve_overhead_tokens_est=(
                        observation.retrieve_overhead_tokens_est
                    ),
                    latency_total_ms=observation.latency_total_ms,
                    latency_compress_ms=observation.latency_compress_ms,
                    latency_upstream_ms=observation.latency_upstream_ms,
                    retrieve_cache_hits=observation.retrieve_cache_hits,
                    retrieve_persistent_hits=observation.retrieve_persistent_hits,
                    retrieve_misses=observation.retrieve_misses,
                    guard_blocks=observation.guard_blocks,
                    semantic_passed=observation.semantic_passed,
                    error_code=observation.error_code,
                    exclusion_reason=observation.exclusion_reason,
                )
            )

    async def put_ledger(self, ledger: DailyTokenLedger) -> None:
        async with self._session() as session, session.begin():
            key = (ledger.utc_date, ledger.model_group)
            row = await session.get(OperationalDailyLedgerRow, key)
            if row is None:
                session.add(
                    OperationalDailyLedgerRow(
                        utc_date=ledger.utc_date,
                        model_group=ledger.model_group,
                        known_tokens=ledger.known_tokens,
                        usage_certain=ledger.usage_certain,
                        source=ledger.source.value,
                    )
                )
            else:
                row.known_tokens = ledger.known_tokens
                row.usage_certain = ledger.usage_certain
                row.source = ledger.source.value

    async def load_dataset(
        self, run_ids: set[str] | None = None
    ) -> tuple[list[RunMetadata], list[RequestObservation]]:
        async with self._session() as session:
            run_statement = select(OperationalRunRow).order_by(
                OperationalRunRow.run_id
            )
            if run_ids is not None:
                run_statement = run_statement.where(
                    OperationalRunRow.run_id.in_(sorted(run_ids))
                )
            run_rows = (await session.execute(run_statement)).scalars().all()
            selected_ids = {row.run_id for row in run_rows}
            if not selected_ids:
                return [], []
            observation_rows = (
                (
                    await session.execute(
                        select(OperationalRequestRecordRow)
                        .where(OperationalRequestRecordRow.run_id.in_(selected_ids))
                        .order_by(
                            OperationalRequestRecordRow.run_id,
                            OperationalRequestRecordRow.logical_request_id,
                            OperationalRequestRecordRow.attempt,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return (
            [self._run_from_row(row) for row in run_rows],
            [self._observation_from_row(row) for row in observation_rows],
        )

    async def get_ledger(
        self, utc_date: date, model_group: str
    ) -> DailyTokenLedger | None:
        async with self._session() as session:
            row = await session.get(
                OperationalDailyLedgerRow,
                (utc_date, model_group),
            )
        if row is None:
            return None
        return DailyTokenLedger(
            utc_date=row.utc_date,
            model_group=row.model_group,
            known_tokens=row.known_tokens,
            usage_certain=row.usage_certain,
            source=LedgerSource(row.source),
        )

    @staticmethod
    def _run_from_row(row: OperationalRunRow) -> RunMetadata:
        def aware(value: datetime | None) -> datetime | None:
            if value is not None and value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        started_at = aware(row.started_at)
        telemetry_expires_at = aware(row.telemetry_expires_at)
        if started_at is None or telemetry_expires_at is None:
            raise ValueError("stored run is missing required UTC timestamps")
        return RunMetadata(
            run_id=row.run_id,
            run_category=RunCategory(row.run_category),
            status=RunStatus(row.status),
            session_id=row.session_id,
            utc_date=row.utc_date,
            started_at=started_at,
            ended_at=aware(row.ended_at),
            commit_sha=row.commit_sha,
            config_hash=row.config_hash,
            provider=row.provider,
            model=row.model,
            compression_mode=CompressionMode(row.compression_mode),
            task_version=row.task_version,
            fault_version=row.fault_version,
            image_digest=row.image_digest,
            project_hash=row.project_hash,
            project_mode=ProjectMode(row.project_mode),
            policy_version=row.policy_version,
            planned_requests=row.planned_requests,
            report_label=ReportLabel(row.report_label),
            actual_data=row.actual_data,
            telemetry_expires_at=telemetry_expires_at,
            retention_policy_version=row.retention_policy_version,
        )

    @staticmethod
    def _observation_from_row(
        row: OperationalRequestRecordRow,
    ) -> RequestObservation:
        return RequestObservation(
            run_id=row.run_id,
            logical_request_id=row.logical_request_id,
            attempt=row.attempt,
            status=ObservationStatus(row.status),
            telemetry_complete=row.telemetry_complete,
            tokens_input_original_est=row.tokens_input_original_est,
            tokens_input_sent_est=row.tokens_input_sent_est,
            tokens_output_est=row.tokens_output_est,
            provider_input_tokens=row.provider_input_tokens,
            provider_cached_input_tokens=row.provider_cached_input_tokens,
            provider_output_tokens=row.provider_output_tokens,
            retrieve_overhead_tokens_est=row.retrieve_overhead_tokens_est,
            latency_total_ms=row.latency_total_ms,
            latency_compress_ms=row.latency_compress_ms,
            latency_upstream_ms=row.latency_upstream_ms,
            retrieve_cache_hits=row.retrieve_cache_hits,
            retrieve_persistent_hits=row.retrieve_persistent_hits,
            retrieve_misses=row.retrieve_misses,
            guard_blocks=row.guard_blocks,
            semantic_passed=row.semantic_passed,
            error_code=row.error_code,
            exclusion_reason=row.exclusion_reason,
        )
