"""telemetry/logger.py 단위 테스트 — SQLite 인메모리 DB 사용."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ccim.telemetry.logger import RequestLogger, RequestRecord
from ccim.telemetry.models import Base, RequestRow


@pytest.fixture
async def logger() -> RequestLogger:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return RequestLogger(engine=engine)


async def test_to_row_basic(logger: RequestLogger) -> None:
    record = RequestRecord(session_id="s1", pcfi_action="allow")
    row = await logger.to_row(record)
    assert isinstance(row, RequestRow)
    assert row.session_id == "s1"
    assert row.pcfi_action == "allow"
    assert row.retrieve_original_calls == 0
    assert row.version == "v1.0"


async def test_to_row_full(logger: RequestLogger) -> None:
    record = RequestRecord(
        session_id="s2",
        pcfi_action="block",
        pcfi_reason="role_switch:U:'ignore'",
        agent_name="cline",
        endpoint="/v1/messages",
        tokens_input_original=5000,
        tokens_input_compressed=3000,
        tokens_output=400,
        latency_ms=120,
        pcfi_latency_ms=8,
        compress_latency_ms=30,
        upstream_latency_ms=80,
        retrieve_original_calls=2,
        write_remaps=1,
        feature_flags={"eval_score": 0.9},
    )
    row = await logger.to_row(record)
    assert row.tokens_input_original == 5000
    assert row.tokens_input_compressed == 3000
    assert row.retrieve_original_calls == 2
    assert row.feature_flags == {"eval_score": 0.9}


async def test_log_inserts_row(logger: RequestLogger) -> None:
    record = RequestRecord(session_id="s3", pcfi_action="allow", tokens_output=100)
    await logger.log(record)

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    async with AsyncSession(logger._engine) as session:
        rows = (await session.execute(select(RequestRow))).scalars().all()

    assert len(rows) == 1
    assert rows[0].session_id == "s3"
    assert rows[0].tokens_output == 100


async def test_log_multiple_rows(logger: RequestLogger) -> None:
    for i in range(5):
        await logger.log(
            RequestRecord(session_id=f"sess_{i}", pcfi_action="allow")
        )

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession
    async with AsyncSession(logger._engine) as session:
        count = (await session.execute(select(func.count(RequestRow.id)))).scalar()

    assert count == 5


async def test_to_row_null_fields_allowed(logger: RequestLogger) -> None:
    """선택 필드가 None이어도 ORM 변환이 정상 동작해야 한다."""
    record = RequestRecord(
        session_id="s_null",
        pcfi_action="allow",
        tokens_input_original=None,
        agent_name=None,
    )
    row = await logger.to_row(record)
    assert row.tokens_input_original is None
    assert row.agent_name is None
