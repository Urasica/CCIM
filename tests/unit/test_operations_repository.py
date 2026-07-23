from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine

from ccim.operations.contracts import DailyTokenLedger, LedgerSource
from ccim.operations.dry_run import DeterministicMockProvider, build_runs
from ccim.operations.models import OperationalRunRow  # noqa: F401
from ccim.operations.repository import OperationalRepository
from ccim.telemetry.models import Base


async def test_repository_roundtrip_run_observation_and_ledger() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = OperationalRepository(engine)
    run = build_runs()[0]
    observation = DeterministicMockProvider().build_observations()[0]
    ledger = DailyTokenLedger(
        utc_date=run.utc_date,
        model_group="gpt-5-mini-family",
        known_tokens=123_000,
        usage_certain=False,
        source=LedgerSource.SIMULATED,
    )

    await repository.create_run(run)
    await repository.record_observation(observation)
    await repository.put_ledger(ledger)
    loaded_runs, loaded_observations = await repository.load_dataset({run.run_id})
    loaded_ledger = await repository.get_ledger(
        ledger.utc_date,
        ledger.model_group,
    )

    assert loaded_runs == [run]
    assert loaded_observations == [observation]
    assert loaded_ledger == ledger
    await engine.dispose()
