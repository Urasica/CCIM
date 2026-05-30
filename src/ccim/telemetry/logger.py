"""요청 1건의 측정값을 PostgreSQL에 저장.

사용 패턴:
    record = RequestRecord(session_id=..., pcfi_action=..., ...)
    await logger.log(record)    # fire-and-forget 권장

설계 §3.3 / §7: V2 평가 결과를 위한 `feature_flags` 컬럼은 dict로 자유 확장.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ccim.telemetry.models import RequestRow


@dataclass
class RequestRecord:
    session_id: str
    pcfi_action: str
    agent_name: str | None = None
    endpoint: str | None = None
    pcfi_reason: str | None = None

    tokens_input_original: int | None = None
    tokens_input_compressed: int | None = None
    tokens_output: int | None = None

    latency_ms: int | None = None
    pcfi_latency_ms: int | None = None
    compress_latency_ms: int | None = None
    upstream_latency_ms: int | None = None

    retrieve_original_calls: int = 0
    write_remaps: int = 0

    feature_flags: dict[str, Any] = field(default_factory=dict)
    version: str = "v1.0"


class RequestLogger:
    """요청 메트릭 라이터. 비동기 SQLAlchemy 사용."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def log(self, record: RequestRecord) -> None:
        """단일 행 INSERT. 실패해도 메인 응답 경로를 막지 않도록 호출자가 fire-and-forget."""
        row = await self.to_row(record)
        async with self._session() as session:
            async with session.begin():
                session.add(row)

    async def to_row(self, record: RequestRecord) -> RequestRow:
        """RequestRecord → ORM 모델 변환."""
        return RequestRow(
            session_id=record.session_id,
            agent_name=record.agent_name,
            endpoint=record.endpoint,
            pcfi_action=record.pcfi_action,
            pcfi_reason=record.pcfi_reason,
            tokens_input_original=record.tokens_input_original,
            tokens_input_compressed=record.tokens_input_compressed,
            tokens_output=record.tokens_output,
            latency_ms=record.latency_ms,
            pcfi_latency_ms=record.pcfi_latency_ms,
            compress_latency_ms=record.compress_latency_ms,
            upstream_latency_ms=record.upstream_latency_ms,
            retrieve_original_calls=record.retrieve_original_calls,
            write_remaps=record.write_remaps,
            feature_flags=record.feature_flags,
            version=record.version,
        )
