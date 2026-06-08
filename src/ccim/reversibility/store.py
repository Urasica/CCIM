"""Redis-backed original-code store.

Key scheme (design section 3.3):
    KEY:   ctx:{session_id}:{context_id}
    VALUE: JSON {original_code, language, line_mapping, created_at}
    TTL:   settings.redis_ttl_seconds
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass
class ContextRecord:
    session_id: str
    context_id: str
    original_code: str
    language: str
    # Whole-document compressed_line -> original_line at the time of compression.
    # The mapping is duplicated across blocks of the same document for V1 simplicity.
    line_mapping: dict[int, int]
    source_path: str | None = None
    symbol_name: str | None = None
    original_lines: tuple[int, int] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def redis_key(self) -> str:
        return f"ctx:{self.session_id}:{self.context_id}"


@dataclass
class ToolResultRecord:
    session_id: str
    content_hash: str
    content: str
    kind: str = "tool_result"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class _RedisLike(Protocol):
    """Subset of redis.asyncio.Redis used by the store. Eases stubbing in tests."""

    async def set(self, name: str, value: str, ex: int | None = ...) -> Any: ...
    async def get(self, name: str) -> Any: ...
    async def delete(self, *names: str) -> Any: ...
    async def sadd(self, name: str, *values: str) -> Any: ...
    async def srem(self, name: str, *values: str) -> Any: ...
    async def smembers(self, name: str) -> Any: ...
    async def expire(self, name: str, time: int) -> Any: ...
    async def ttl(self, name: str) -> Any: ...
    async def memory_usage(self, name: str) -> Any: ...


@dataclass
class ContextStoreEntry:
    session_id: str
    context_id: str
    redis_key: str
    language: str | None
    source_path: str | None
    symbol_name: str | None
    original_lines: tuple[int, int] | None
    original_chars: int
    ttl_seconds: int | None
    memory_bytes_est: int | None
    created_at: datetime | None


class ReversibilityStore:
    """Async wrapper around Redis using JSON-serialized values.

    V1 keeps it simple with SET/GET; RedisJSON / Vector Search are deferred to V3.
    """

    def __init__(self, redis: _RedisLike, ttl_seconds: int = 3600) -> None:
        self._r = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: str, context_id: str) -> str:
        return f"ctx:{session_id}:{context_id}"

    @staticmethod
    def _tool_key(session_id: str, content_hash: str) -> str:
        return f"tool:{session_id}:{content_hash}"

    @staticmethod
    def _context_index_key(session_id: str) -> str:
        return f"idx:ctx:{session_id}"

    async def put(self, record: ContextRecord) -> None:
        payload = {
            "original_code": record.original_code,
            "language": record.language,
            # JSON object keys must be strings; restore to int on read.
            "line_mapping": {str(k): v for k, v in record.line_mapping.items()},
            "source_path": record.source_path,
            "symbol_name": record.symbol_name,
            "original_lines": list(record.original_lines) if record.original_lines else None,
            "created_at": record.created_at.isoformat(),
        }
        await self._r.set(
            self._key(record.session_id, record.context_id),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )
        index_key = self._context_index_key(record.session_id)
        await _maybe_call(self._r, "sadd", index_key, record.context_id)
        await _maybe_call(self._r, "expire", index_key, self._ttl)

    async def get(self, session_id: str, context_id: str) -> ContextRecord | None:
        raw = await self._r.get(self._key(session_id, context_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return ContextRecord(
            session_id=session_id,
            context_id=context_id,
            original_code=data["original_code"],
            language=data["language"],
            line_mapping={int(k): v for k, v in data.get("line_mapping", {}).items()},
            source_path=data.get("source_path"),
            symbol_name=data.get("symbol_name"),
            original_lines=(
                tuple(data["original_lines"])
                if isinstance(data.get("original_lines"), list)
                and len(data["original_lines"]) == 2
                else None
            ),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_line_mapping(
        self, session_id: str, context_id: str
    ) -> dict[int, int] | None:
        """Convenience accessor for Write Mapper. Returns None on miss."""
        record = await self.get(session_id, context_id)
        return record.line_mapping if record else None

    async def delete(self, session_id: str, context_id: str) -> None:
        await self._r.delete(self._key(session_id, context_id))
        await _maybe_call(self._r, "srem", self._context_index_key(session_id), context_id)

    async def list_context_ids(self, session_id: str) -> list[str]:
        raw_ids = await _maybe_call(self._r, "smembers", self._context_index_key(session_id))
        if raw_ids is None:
            return []
        return sorted(_decode_redis_text(item) for item in raw_ids)

    async def list_contexts(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[ContextStoreEntry]:
        entries = []
        for context_id in await self.list_context_ids(session_id):
            key = self._key(session_id, context_id)
            raw = await self._r.get(key)
            if raw is None:
                await _maybe_call(self._r, "srem", self._context_index_key(session_id), context_id)
                continue
            text = _decode_redis_text(raw)
            data = json.loads(text)
            memory_bytes = await _maybe_call(self._r, "memory_usage", key)
            ttl_seconds = await _maybe_call(self._r, "ttl", key)
            entries.append(
                ContextStoreEntry(
                    session_id=session_id,
                    context_id=context_id,
                    redis_key=key,
                    language=data.get("language"),
                    source_path=data.get("source_path"),
                    symbol_name=data.get("symbol_name"),
                    original_lines=(
                        tuple(data["original_lines"])
                        if isinstance(data.get("original_lines"), list)
                        and len(data["original_lines"]) == 2
                        else None
                    ),
                    original_chars=len(data.get("original_code") or ""),
                    ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                    memory_bytes_est=(
                        memory_bytes if isinstance(memory_bytes, int) else len(text.encode("utf-8"))
                    ),
                    created_at=(
                        datetime.fromisoformat(data["created_at"])
                        if data.get("created_at")
                        else None
                    ),
                )
            )
        entries.sort(
            key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return entries[:limit] if limit is not None else entries

    async def put_tool_result(self, record: ToolResultRecord) -> None:
        payload = {
            "content": record.content,
            "kind": record.kind,
            "metadata": record.metadata,
            "created_at": record.created_at.isoformat(),
        }
        await self._r.set(
            self._tool_key(record.session_id, record.content_hash),
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl,
        )

    async def get_tool_result(
        self, session_id: str, content_hash: str
    ) -> ToolResultRecord | None:
        raw = await self._r.get(self._tool_key(session_id, content_hash))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return ToolResultRecord(
            session_id=session_id,
            content_hash=content_hash,
            content=data["content"],
            kind=data.get("kind", "tool_result"),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


def _decode_redis_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


async def _maybe_call(target: Any, method_name: str, *args: Any) -> Any:
    method = getattr(target, method_name, None)
    if method is None:
        return None
    return await method(*args)
