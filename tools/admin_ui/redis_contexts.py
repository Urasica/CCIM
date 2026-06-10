"""Redis context-store inspection helpers for the admin UI."""

from __future__ import annotations

from typing import Any

from ccim.reversibility.store import ReversibilityStore

from .settings import effective_env, mask_secret_url

INDEX_PREFIX = "idx:ctx:"


async def context_overview(limit_sessions: int = 20, limit_contexts: int = 200) -> dict[str, Any]:
    env = effective_env()
    url = env.get("CCIM_REDIS_URL", "redis://localhost:6379/0")
    ttl_seconds = int(env.get("CCIM_REDIS_TTL_SECONDS") or 3600)
    evidence_store_path = env.get("CCIM_EVIDENCE_STORE_PATH", "").strip()
    try:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(url, decode_responses=False)
        try:
            await redis.ping()
            sessions = await _indexed_sessions(redis, limit_sessions)
            store = ReversibilityStore(redis=redis, ttl_seconds=ttl_seconds)
            session_rows = []
            total_contexts = 0
            total_memory = 0
            min_ttl: int | None = None
            for session_id in sessions:
                entries = await store.list_contexts(session_id, limit=limit_contexts)
                contexts = [_entry_to_dict(entry) for entry in entries]
                total_contexts += len(contexts)
                memory = sum(item["memory_bytes_est"] or 0 for item in contexts)
                total_memory += memory
                ttls = [
                    item["ttl_seconds"]
                    for item in contexts
                    if isinstance(item.get("ttl_seconds"), int) and item["ttl_seconds"] >= 0
                ]
                if ttls:
                    session_min_ttl = min(ttls)
                    min_ttl = session_min_ttl if min_ttl is None else min(min_ttl, session_min_ttl)
                session_rows.append(
                    {
                        "session_id": session_id,
                        "context_count": len(contexts),
                        "memory_bytes_est": memory,
                        "min_ttl_seconds": min(ttls) if ttls else None,
                        "contexts": contexts,
                    }
                )
            return {
                "ok": True,
                "url": mask_secret_url(url),
                "evidence_store_enabled": bool(evidence_store_path),
                "evidence_store_path": evidence_store_path,
                "session_count": len(session_rows),
                "context_count": total_contexts,
                "memory_bytes_est": total_memory,
                "min_ttl_seconds": min_ttl,
                "sessions": session_rows,
            }
        finally:
            await redis.aclose()
    except Exception as exc:
        return {
            "ok": False,
            "url": mask_secret_url(url),
            "evidence_store_enabled": bool(evidence_store_path),
            "evidence_store_path": evidence_store_path,
            "message": f"{type(exc).__name__}: {exc}",
            "session_count": 0,
            "context_count": 0,
            "memory_bytes_est": 0,
            "min_ttl_seconds": None,
            "sessions": [],
        }


async def _indexed_sessions(redis: Any, limit: int) -> list[str]:
    sessions = []
    async for raw_key in redis.scan_iter(f"{INDEX_PREFIX}*", count=100):
        key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
        if key.startswith(INDEX_PREFIX):
            sessions.append(key.removeprefix(INDEX_PREFIX))
        if len(sessions) >= limit:
            break
    return sorted(sessions)


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "session_id": entry.session_id,
        "context_id": entry.context_id,
        "redis_key": entry.redis_key,
        "language": entry.language,
        "source_path": entry.source_path,
        "symbol_name": entry.symbol_name,
        "original_lines": list(entry.original_lines) if entry.original_lines else None,
        "document_id": entry.document_id,
        "document_hash": entry.document_hash,
        "document_version": entry.document_version,
        "span_type": entry.span_type,
        "source_kind": entry.source_kind,
        "source_uri": entry.source_uri,
        "original_chars": entry.original_chars,
        "ttl_seconds": entry.ttl_seconds,
        "memory_bytes_est": entry.memory_bytes_est,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }
