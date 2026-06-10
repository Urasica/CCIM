"""FastAPI 엔트리포인트. lifespan에서 의존성(Redis/PG/LLM client) 초기화."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from ccim import __version__
from ccim.api.routes import router as messages_router
from ccim.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """게이트웨이 부팅·종료 훅.

    부팅:
      - Redis 연결 풀 생성
      - PostgreSQL 엔진 생성 + 헬스체크
      - LLM client 초기화 (provider 설정 기반)
      - OpenTelemetry tracer/meter 등록
      - 미들웨어 체인 조립 → app.state.chain

    종료:
      - DB 엔진 dispose
      - HTTP 클라이언트 aclose
      - Redis pool close
    """
    settings = get_settings()

    # ── Redis ────────────────────────────────────────────────────────
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
        await redis_client.ping()
        logger.info("Redis connected: %s", settings.redis_url)
    except Exception as exc:
        logger.warning("Redis unavailable (%s) — reversibility disabled", exc)
        redis_client = None  # type: ignore[assignment]

    # ── PostgreSQL ───────────────────────────────────────────────────
    db_engine = None
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        logger.info("PostgreSQL connecting: %s", settings.database_url)
        db_engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=False,
        )
        # 헬스체크
        async with db_engine.connect() as conn:
            from sqlalchemy import text
            result = await conn.execute(text("SELECT version()"))
            pg_ver = result.scalar()
            logger.info("PostgreSQL connected: %s", pg_ver)
    except Exception:
        logger.warning("PostgreSQL unavailable — telemetry disabled", exc_info=True)

    # ── LLM client ──────────────────────────────────────────────────
    from ccim.llm.client import build_client
    llm_provider = settings.llm_provider
    llm_base_url = settings.llm_base_url or (
        settings.anthropic_base_url if llm_provider == "anthropic" else None
    )
    llm_api_key = (
        settings.anthropic_api_key
        if llm_provider == "anthropic"
        else settings.openai_api_key
    )
    llm_client = build_client(
        provider=llm_provider,
        api_key=llm_api_key,
        base_url=llm_base_url,
        timeout_s=settings.llm_timeout_s,
    )
    logger.info("LLM client: provider=%s base_url=%s timeout=%.0fs", llm_provider, llm_base_url, settings.llm_timeout_s)

    # ── 의존성 인스턴스 구성 ─────────────────────────────────────────
    from ccim.compress.ast_compressor import ASTCompressor
    from ccim.middleware.chain import (
        CompressMiddleware,
        CurrentTurnWriteGuardMiddleware,
        ForwardAndInterceptMiddleware,
        MiddlewareChain,
        OrphanMarkerScanMiddleware,
        PCFIMiddleware,
        TelemetryMiddleware,
        WriteRemapMiddleware,
    )
    from ccim.pcfi.enforcer import PCFIEnforcer
    from ccim.reversibility.interceptor import ReversibilityInterceptor
    from ccim.reversibility.store import ReversibilityStore
    from ccim.telemetry.logger import RequestLogger
    from ccim.write_mapper.mapper import WriteMapper

    # PCFI
    guard = None
    if settings.llamaguard_url:
        try:
            from ccim.pcfi.llama_guard import LlamaGuardClient
            guard = LlamaGuardClient(
                base_url=settings.llamaguard_url,
                model=settings.llamaguard_model,
            )
            logger.info("Llama Guard: %s / %s", settings.llamaguard_url, settings.llamaguard_model)
        except Exception as exc:
            logger.warning("Llama Guard init failed (%s) — regex-only mode", exc)
    skip_cats = {
        c.strip().upper()
        for c in settings.pcfi_skip_guard_categories.split(",")
        if c.strip()
    }
    if skip_cats:
        logger.info("PCFI: skip guard categories = %s", skip_cats)
    pcfi_enforcer = PCFIEnforcer(guard=guard, skip_guard_categories=skip_cats)

    # Reversibility
    persistent_store = None
    if settings.evidence_store_path:
        try:
            from ccim.reversibility.persistent import SQLiteEvidenceStore

            persistent_store = SQLiteEvidenceStore(settings.evidence_store_path)
            logger.info("Evidence persistent store: %s", settings.evidence_store_path)
        except Exception:
            logger.warning("Evidence persistent store unavailable", exc_info=True)
    store = ReversibilityStore(
        redis=redis_client if redis_client else _NullRedis(),
        ttl_seconds=settings.redis_ttl_seconds,
        persistent_store=persistent_store,
    )
    interceptor = ReversibilityInterceptor(store=store)
    mapper = WriteMapper(store=store)

    # Compressor
    compressor = ASTCompressor()

    # Telemetry logger
    req_logger: RequestLogger | None = None
    if db_engine is not None:
        req_logger = RequestLogger(engine=db_engine)

    compression_runtime_enabled = (
        redis_client is not None and settings.compression_enabled
    )

    # ── 미들웨어 체인 조립 ─────────────────────────────────────────
    stages = [
        PCFIMiddleware(enforcer=pcfi_enforcer),
        CompressMiddleware(
            compressor=compressor,
            store=store,
            settings=settings,
            compress_enabled=compression_runtime_enabled,
        ),
        ForwardAndInterceptMiddleware(llm_client=llm_client, interceptor=interceptor, model_override=settings.llm_model),
        CurrentTurnWriteGuardMiddleware(settings=settings),
        OrphanMarkerScanMiddleware(store=store),
        WriteRemapMiddleware(mapper=mapper),
        TelemetryMiddleware(logger=req_logger or _NullLogger()),
    ]
    app.state.chain = MiddlewareChain(stages=stages)
    app.state.llm_client = llm_client
    app.state.redis = redis_client
    app.state.db_engine = db_engine
    app.state.compression_enabled = compression_runtime_enabled
    app.state.telemetry_enabled = req_logger is not None

    logger.info("CCIM Gateway v%s ready (chain: %s)", __version__, " → ".join(s.name for s in stages))

    yield

    # ── 종료 정리 ────────────────────────────────────────────────────
    with suppress(Exception):
        await llm_client.aclose()
    if guard is not None:
        with suppress(Exception):
            await guard.aclose()
    if db_engine is not None:
        with suppress(Exception):
            await db_engine.dispose()
    if redis_client is not None:
        with suppress(Exception):
            await redis_client.aclose()
    logger.info("CCIM Gateway shut down.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CCIM Gateway",
        version=__version__,
        description="Coding-agent Context & Integrity Middleware — V1 Foundation",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": settings.version,
            "redis_connected": getattr(app.state, "redis", None) is not None,
            "postgres_connected": getattr(app.state, "db_engine", None) is not None,
            "compression_enabled": bool(getattr(app.state, "compression_enabled", False)),
            "telemetry_enabled": bool(getattr(app.state, "telemetry_enabled", False)),
        }

    app.include_router(messages_router)

    # OpenTelemetry (실패해도 기동 유지)
    from ccim.telemetry.otel import setup_otel
    setup_otel(
        app,
        service_name=settings.otel_service_name,
        exporter_endpoint=settings.otel_exporter_endpoint,
    )

    return app


app = create_app()


def run() -> None:
    """`ccim` 콘솔 스크립트 진입점."""
    import asyncio
    import sys

    import uvicorn

    settings = get_settings()

    if sys.platform == "win32":
        # uvicorn이 내부적으로 ProactorEventLoop를 재생성하므로
        # policy 변경만으로는 부족 → SelectorEventLoop를 직접 만들어서 주입
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(
            "ccim.main:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            loop="none",  # 위에서 직접 설정한 루프 사용
        )
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(
            "ccim.main:app",
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )


# ────────────────────────────────────────────────────────────────────
# Null 객체 — 인프라 미연결 시 graceful 폴백
# ────────────────────────────────────────────────────────────────────


class _NullRedis:
    """Redis가 없을 때 store가 조용히 동작하도록 하는 stub."""

    async def set(self, name: str, value: str, ex: int | None = None) -> None:
        pass

    async def get(self, name: str) -> None:
        return None

    async def delete(self, *names: str) -> None:
        pass

    async def sadd(self, name: str, *values: str) -> None:
        pass

    async def srem(self, name: str, *values: str) -> None:
        pass

    async def smembers(self, name: str) -> set[str]:
        return set()

    async def expire(self, name: str, time: int) -> None:
        pass

    async def ttl(self, name: str) -> int:
        return -2

    async def memory_usage(self, name: str) -> None:
        return None


class _NullLogger:
    """Telemetry가 비활성화된 경우 사용하는 no-op logger."""

    async def log(self, record: object) -> None:
        pass
