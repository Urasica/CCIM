"""Read-only deployment diagnostics for ``ccim doctor``."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

DoctorStatus = Literal["pass", "fail", "skipped"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    required: bool
    reason: str

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


async def collect_doctor_report(
    settings: Any,
    *,
    offline: bool = False,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Inspect configured dependencies without applying migrations or changing state."""
    checks: list[DoctorCheck] = [_check_configuration(settings)]
    checks.append(await _check_port(settings, timeout_s=timeout_s))

    if offline:
        checks.extend(
            [
                DoctorCheck("redis", "skipped", True, "offline"),
                DoctorCheck("postgres", "skipped", True, "offline"),
                DoctorCheck("migrations", "skipped", True, "offline"),
                DoctorCheck("provider", "skipped", True, "offline"),
            ]
        )
    else:
        checks.append(await _check_redis(settings, timeout_s=timeout_s))
        postgres, migrations = await _check_postgres_and_migrations(
            settings,
            timeout_s=timeout_s,
        )
        checks.extend([postgres, migrations])
        checks.append(await _check_provider(settings, timeout_s=timeout_s))

    redis_ready = next(
        (check.status == "pass" for check in checks if check.name == "redis"),
        False,
    )
    if settings.compression_enabled:
        checks.append(
            DoctorCheck(
                "compression",
                "pass" if redis_ready else ("skipped" if offline else "fail"),
                True,
                "redis_ready" if redis_ready else ("offline" if offline else "redis_unavailable"),
            )
        )
    else:
        checks.append(DoctorCheck("compression", "pass", False, "disabled"))

    counts = {
        status: sum(check.status == status for check in checks)
        for status in ("pass", "fail", "skipped")
    }
    ok = all(check.status == "pass" for check in checks if check.required)
    return {
        "schema_version": "1",
        "command": "ccim doctor",
        "ok": ok,
        "checks": [check.as_json() for check in checks],
        "summary": counts,
    }


def _check_configuration(settings: Any) -> DoctorCheck:
    providers = {"anthropic", "openai", "openai-compatible"}
    if settings.llm_provider not in providers:
        return DoctorCheck("configuration", "fail", True, "unknown_provider")
    if settings.llm_provider == "openai-compatible" and not settings.llm_base_url:
        return DoctorCheck(
            "configuration",
            "fail",
            True,
            "openai_compatible_base_url_missing",
        )
    for value in (settings.redis_url, settings.database_url):
        parsed = urlparse(value)
        if not parsed.scheme:
            return DoctorCheck("configuration", "fail", True, "invalid_dependency_url")
    return DoctorCheck("configuration", "pass", True, "valid")


async def _check_port(settings: Any, *, timeout_s: float) -> DoctorCheck:
    target_host = settings.host
    if target_host in {"0.0.0.0", "::"}:
        target_host = "127.0.0.1"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"http://{target_host}:{settings.port}/live")
        payload = response.json()
        if response.status_code == 200 and payload.get("status") == "live":
            return DoctorCheck("port", "pass", True, "gateway_live")
    except Exception:
        pass

    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((settings.host, settings.port))
    except OSError:
        return DoctorCheck("port", "fail", True, "port_in_use_by_other_process")
    finally:
        probe.close()
    return DoctorCheck("port", "pass", True, "available")


async def _check_redis(settings: Any, *, timeout_s: float) -> DoctorCheck:
    client = None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_connect_timeout=timeout_s,
            socket_timeout=timeout_s,
        )
        await asyncio.wait_for(client.ping(), timeout=timeout_s)
    except Exception as exc:
        return DoctorCheck(
            "redis",
            "fail",
            True,
            f"connection_failed:{type(exc).__name__}",
        )
    finally:
        if client is not None:
            with suppress(Exception):
                await client.aclose()
    return DoctorCheck("redis", "pass", True, "ping_ok")


async def _check_postgres_and_migrations(
    settings: Any,
    *,
    timeout_s: float,
) -> tuple[DoctorCheck, DoctorCheck]:
    engine = None
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        async with asyncio.timeout(timeout_s):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            from ccim.migrations import discover_migrations, inspect_async_engine

            state = await inspect_async_engine(engine, discover_migrations())
    except Exception as exc:
        reason = f"connection_failed:{type(exc).__name__}"
        return (
            DoctorCheck("postgres", "fail", True, reason),
            DoctorCheck("migrations", "skipped", True, "postgres_unavailable"),
        )
    finally:
        if engine is not None:
            with suppress(Exception):
                await engine.dispose()
    return (
        DoctorCheck("postgres", "pass", True, "query_ok"),
        DoctorCheck(
            "migrations",
            "pass" if state.current else "fail",
            True,
            state.status,
        ),
    )


async def _check_provider(settings: Any, *, timeout_s: float) -> DoctorCheck:
    client = None
    try:
        from ccim.llm.client import build_client

        base_url = settings.llm_base_url or (
            settings.anthropic_base_url
            if settings.llm_provider == "anthropic"
            else None
        )
        api_key = (
            settings.anthropic_api_key
            if settings.llm_provider == "anthropic"
            else settings.openai_api_key
        )
        client = build_client(
            provider=settings.llm_provider,
            api_key=api_key,
            base_url=base_url,
            timeout_s=timeout_s,
        )
        payload = await asyncio.wait_for(client.list_models(), timeout=timeout_s)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return DoctorCheck("provider", "fail", True, "invalid_models_response")
    except Exception as exc:
        return DoctorCheck(
            "provider",
            "fail",
            True,
            f"models_check_failed:{type(exc).__name__}",
        )
    finally:
        if client is not None:
            with suppress(Exception):
                await client.aclose()
    return DoctorCheck("provider", "pass", True, "models_read_ok")
