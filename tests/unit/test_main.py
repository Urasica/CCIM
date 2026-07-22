from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


class _DummyClient:
    async def aclose(self) -> None:
        pass


class _DummyRedis:
    async def ping(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


async def test_lifespan_survives_postgres_unavailable() -> None:
    import ccim.main as main

    app = SimpleNamespace(state=SimpleNamespace())

    with (
        patch("ccim.llm.client.build_client", return_value=_DummyClient()),
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=RuntimeError("db down"),
        ),
    ):
        async with main.lifespan(app):
            assert hasattr(app.state, "chain")


async def test_lifespan_closes_llamaguard_client() -> None:
    import ccim.main as main

    class _DummyGuard:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    app = SimpleNamespace(state=SimpleNamespace())
    guard = _DummyGuard()
    settings = main.get_settings().model_copy(update={"llamaguard_url": "http://guard"})

    with (
        patch("ccim.main.get_settings", return_value=settings),
        patch("redis.asyncio.from_url", return_value=_DummyRedis()),
        patch("ccim.llm.client.build_client", return_value=_DummyClient()),
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "ccim.pcfi.llama_guard.LlamaGuardClient",
            return_value=guard,
        ),
    ):
        async with main.lifespan(app):
            pass

    assert guard.closed is True


async def test_lifespan_respects_global_compression_disabled() -> None:
    import ccim.main as main

    app = SimpleNamespace(state=SimpleNamespace())
    settings = main.get_settings().model_copy(update={"compression_enabled": False})

    with (
        patch("ccim.main.get_settings", return_value=settings),
        patch("redis.asyncio.from_url", return_value=_DummyRedis()),
        patch("ccim.llm.client.build_client", return_value=_DummyClient()),
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=RuntimeError("db down"),
        ),
    ):
        async with main.lifespan(app):
            assert app.state.compression_enabled is False


def test_readiness_payload_is_ready_with_current_dependencies() -> None:
    import ccim.main as main

    class _Telemetry:
        def snapshot(self) -> dict[str, int | bool]:
            return {
                "enabled": True,
                "pending": 0,
                "scheduled": 4,
                "succeeded": 4,
                "failed": 0,
                "dropped": 0,
                "skipped": 0,
                "drain_timeouts": 0,
            }

    app = SimpleNamespace(
        state=SimpleNamespace(
            redis=object(),
            db_engine=object(),
            migration_state={"status": "current", "current": True},
            compression_enabled=True,
            telemetry_enabled=True,
            telemetry_runtime=_Telemetry(),
        )
    )

    payload, status_code = main._readiness_payload(app, main.get_settings())

    assert status_code == 200
    assert payload["status"] == "ready"
    assert payload["dependencies"]["migrations"]["current"] is True
    assert payload["telemetry"]["succeeded"] == 4


def test_readiness_payload_reports_migration_degraded() -> None:
    import ccim.main as main

    app = SimpleNamespace(
        state=SimpleNamespace(
            redis=object(),
            db_engine=object(),
            migration_state={"status": "outdated", "current": False},
            compression_enabled=True,
            telemetry_enabled=False,
        )
    )

    payload, status_code = main._readiness_payload(app, main.get_settings())

    assert status_code == 503
    assert payload["status"] == "degraded"
    assert payload["dependencies"]["migrations"]["status"] == "outdated"
