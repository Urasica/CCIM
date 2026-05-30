"""Dependency health checks for Redis, PostgreSQL, and CCIM HTTP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from .settings import effective_env, mask_secret_url


def dep_result(ok: bool, url: str, message: str) -> dict[str, Any]:
    return {"ok": ok, "url": mask_secret_url(url), "message": message}


def env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def check_redis(url: str) -> dict[str, Any]:
    try:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(url, decode_responses=False)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
        return dep_result(True, url, "connected")
    except Exception as exc:
        return dep_result(False, url, f"{type(exc).__name__}: {exc}")


async def check_postgres(url: str) -> dict[str, Any]:
    try:
        import psycopg

        sync_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        with psycopg.connect(sync_url, connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return dep_result(True, url, "connected")
    except Exception as exc:
        return dep_result(False, url, f"{type(exc).__name__}: {exc}")


async def check_ccim_http(
    env: dict[str, str],
    is_running: Callable[[], bool],
) -> dict[str, Any]:
    host = env.get("CCIM_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = env.get("CCIM_PORT", "8080")
    url = f"http://{host}:{port}/health"
    if not is_running():
        return dep_result(False, url, "ccim process is stopped")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
        if response.status_code == 200:
            health = response.json()
            if "compression_enabled" not in health or "telemetry_enabled" not in health:
                return dep_result(
                    False,
                    url,
                    "healthy but missing v2 dependency fields; stale or foreign CCIM is listening on this port",
                )
            compression_enabled = bool(health.get("compression_enabled"))
            telemetry_enabled = bool(health.get("telemetry_enabled"))
            compression_config_enabled = env_bool(env, "CCIM_COMPRESSION_ENABLED", True)
            if not compression_enabled and not compression_config_enabled and telemetry_enabled:
                return dep_result(
                    True,
                    url,
                    "healthy; compression disabled by setting and telemetry enabled",
                )
            if not compression_enabled or not telemetry_enabled:
                disabled = []
                if not compression_enabled:
                    disabled.append("compression")
                if not telemetry_enabled:
                    disabled.append("telemetry")
                return dep_result(
                    False,
                    url,
                    "healthy but "
                    + "/".join(disabled)
                    + " disabled in running process; restart CCIM",
                )
            return dep_result(True, url, "healthy; compression and telemetry enabled")
        return dep_result(False, url, f"HTTP {response.status_code}")
    except Exception as exc:
        return dep_result(False, url, f"{type(exc).__name__}: {exc}")


async def dependency_status(is_running: Callable[[], bool]) -> dict[str, Any]:
    env = effective_env()
    compression_config_enabled = env_bool(env, "CCIM_COMPRESSION_ENABLED", True)
    redis_url = env.get("CCIM_REDIS_URL", "redis://localhost:6379/0")
    database_url = env.get(
        "CCIM_DATABASE_URL",
        "postgresql+psycopg://ccim:ccim@localhost:5432/ccim",
    )
    redis = await check_redis(redis_url)
    postgres = await check_postgres(database_url)
    ccim_http = await check_ccim_http(env, is_running)
    return {
        "redis": redis,
        "postgres": postgres,
        "ccim_http": ccim_http,
        "ready_for_compression": bool(redis["ok"]),
        "ready_for_measure": bool(postgres["ok"]),
        "start_blocked": not (
            postgres["ok"] and (redis["ok"] or not compression_config_enabled)
        ),
    }


def ensure_dependencies_ready(dependencies: dict[str, Any]) -> None:
    if not dependencies["start_blocked"]:
        return
    failed = [name for name in ("redis", "postgres") if not dependencies[name]["ok"]]
    raise HTTPException(
        status_code=409,
        detail=(
            "dependency check failed before starting CCIM: "
            + ", ".join(failed)
            + ". Start docker dependencies and retry. "
            + "Redis is required when compression is enabled; PostgreSQL is required for telemetry/measure."
        ),
    )
