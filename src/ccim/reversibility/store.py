"""Redis-backed original evidence store.

Key scheme (design section 3.3):
    KEY:   ctx:{session_id}:{context_id}
    VALUE: JSON {original_code, language, line_mapping, evidence metadata, created_at}
    TTL:   settings.redis_ttl_seconds
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

DEFAULT_DOCUMENT_VERSION = 1
DEFAULT_SPAN_TYPE = "code_symbol"
DEFAULT_SOURCE_KIND = "code"


def normalize_evidence_text(text: str) -> str:
    """Normalize text for content-addressed document hashing."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compute_document_hash(text: str) -> str:
    """Return a stable SHA-256 hash for evidence document reuse checks."""
    return sha256(normalize_evidence_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceIdentity:
    """Stable identity for a source document inside an evidence workflow."""

    document_id: str
    document_hash: str
    document_version: int = DEFAULT_DOCUMENT_VERSION

    def __post_init__(self) -> None:
        if not self.document_id:
            raise ValueError("document_id must not be empty")
        if not self.document_hash:
            raise ValueError("document_hash must not be empty")
        if self.document_version < 1:
            raise ValueError("document_version must be >= 1")


@dataclass
class EvidenceSpan:
    """Generic evidence span model used to generalize code contexts over time.

    P0 keeps this model compatible with the existing Redis `ContextRecord`.
    Persistent storage and reload behavior are intentionally deferred to P1.
    """

    session_id: str
    document_id: str
    document_hash: str
    document_version: int
    span_id: str
    span_type: str
    source_kind: str
    source_uri: str | None
    original_text: str
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def context_id(self) -> str:
        return self.span_id

    @property
    def full_context_id(self) -> str:
        return f"{self.session_id}:{self.span_id}"

    @classmethod
    def from_context_record(cls, record: ContextRecord) -> EvidenceSpan:
        document_id = record.document_id or record.source_path or record.context_id
        document_hash = record.document_hash or compute_document_hash(record.original_code)
        line_start = record.original_lines[0] if record.original_lines else None
        line_end = record.original_lines[1] if record.original_lines else None
        return cls(
            session_id=record.session_id,
            document_id=document_id,
            document_hash=document_hash,
            document_version=record.document_version or DEFAULT_DOCUMENT_VERSION,
            span_id=record.context_id,
            span_type=record.span_type or DEFAULT_SPAN_TYPE,
            source_kind=record.source_kind or DEFAULT_SOURCE_KIND,
            source_uri=record.source_uri or record.source_path,
            original_text=record.original_code,
            line_start=line_start,
            line_end=line_end,
            metadata=dict(record.metadata),
            created_at=record.created_at,
        )

    def to_context_record(
        self,
        *,
        language: str = "text",
        line_mapping: dict[int, int] | None = None,
    ) -> ContextRecord:
        original_lines = (
            (self.line_start, self.line_end)
            if self.line_start is not None and self.line_end is not None
            else None
        )
        return ContextRecord(
            session_id=self.session_id,
            context_id=self.span_id,
            original_code=self.original_text,
            language=language,
            line_mapping=line_mapping or {},
            source_path=self.source_uri,
            original_lines=original_lines,
            document_id=self.document_id,
            document_hash=self.document_hash,
            document_version=self.document_version,
            span_type=self.span_type,
            source_kind=self.source_kind,
            source_uri=self.source_uri,
            metadata=dict(self.metadata),
            created_at=self.created_at,
        )


def _context_record_to_payload(record: ContextRecord) -> dict[str, Any]:
    return {
        "original_code": record.original_code,
        "language": record.language,
        # JSON object keys must be strings; restore to int on read.
        "line_mapping": {str(k): v for k, v in record.line_mapping.items()},
        "source_path": record.source_path,
        "symbol_name": record.symbol_name,
        "original_lines": list(record.original_lines) if record.original_lines else None,
        "document_id": record.document_id,
        "document_hash": record.document_hash,
        "document_version": record.document_version,
        "span_type": record.span_type,
        "source_kind": record.source_kind,
        "source_uri": record.source_uri,
        "metadata": record.metadata,
        "created_at": record.created_at.isoformat(),
    }


def _context_record_from_payload(
    session_id: str,
    context_id: str,
    data: dict[str, Any],
) -> ContextRecord:
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
        document_id=data.get("document_id"),
        document_hash=data.get("document_hash"),
        document_version=data.get("document_version"),
        span_type=data.get("span_type") or DEFAULT_SPAN_TYPE,
        source_kind=data.get("source_kind") or DEFAULT_SOURCE_KIND,
        source_uri=data.get("source_uri") or data.get("source_path"),
        metadata=data.get("metadata") or {},
        created_at=datetime.fromisoformat(data["created_at"]),
    )


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
    document_id: str | None = None
    document_hash: str | None = None
    document_version: int | None = None
    span_type: str = DEFAULT_SPAN_TYPE
    source_kind: str = DEFAULT_SOURCE_KIND
    source_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def redis_key(self) -> str:
        return f"ctx:{self.session_id}:{self.context_id}"

    def to_evidence_span(self) -> EvidenceSpan:
        return EvidenceSpan.from_context_record(self)


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


class PersistentEvidenceStore(Protocol):
    """Persistent backing store used to warm Redis after cache loss."""

    async def put_context(self, record: ContextRecord) -> None: ...
    async def get_context(self, session_id: str, context_id: str) -> ContextRecord | None: ...
    async def get_contexts_by_document_hash(self, document_hash: str) -> list[ContextRecord]: ...


@dataclass
class StoreStats:
    redis_hits: int = 0
    redis_misses: int = 0
    persistent_hits: int = 0
    persistent_misses: int = 0
    persistent_write_failures: int = 0
    redis_warm_loads: int = 0
    redis_warm_load_failures: int = 0

    def snapshot(self) -> StoreStats:
        return StoreStats(**self.__dict__)


@dataclass
class ContextStoreEntry:
    session_id: str
    context_id: str
    redis_key: str
    language: str | None
    source_path: str | None
    symbol_name: str | None
    original_lines: tuple[int, int] | None
    document_id: str | None
    document_hash: str | None
    document_version: int | None
    span_type: str | None
    source_kind: str | None
    source_uri: str | None
    original_chars: int
    ttl_seconds: int | None
    memory_bytes_est: int | None
    created_at: datetime | None


class ReversibilityStore:
    """Async wrapper around Redis using JSON-serialized values.

    V1 keeps it simple with SET/GET; RedisJSON / Vector Search are deferred to V3.
    """

    def __init__(
        self,
        redis: _RedisLike,
        ttl_seconds: int = 3600,
        persistent_store: PersistentEvidenceStore | None = None,
    ) -> None:
        self._r = redis
        self._ttl = ttl_seconds
        self._persistent = persistent_store
        self._stats = StoreStats()

    @property
    def stats(self) -> StoreStats:
        return self._stats

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
        await self._put_redis(record)
        if self._persistent is not None:
            try:
                await self._persistent.put_context(record)
            except Exception:
                self._stats.persistent_write_failures += 1

    async def _put_redis(self, record: ContextRecord) -> None:
        payload = _context_record_to_payload(record)
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
        if raw is not None:
            self._stats.redis_hits += 1
            text = _decode_redis_text(raw)
            data = json.loads(text)
            return _context_record_from_payload(session_id, context_id, data)

        self._stats.redis_misses += 1
        if self._persistent is None:
            return None

        record = await self._persistent.get_context(session_id, context_id)
        if record is None:
            self._stats.persistent_misses += 1
            return None

        self._stats.persistent_hits += 1
        try:
            await self._put_redis(record)
            self._stats.redis_warm_loads += 1
        except Exception:
            self._stats.redis_warm_load_failures += 1
        return record

    async def get_contexts_by_document_hash(self, document_hash: str) -> list[ContextRecord]:
        if self._persistent is None:
            return []
        return await self._persistent.get_contexts_by_document_hash(document_hash)

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
                    document_id=data.get("document_id"),
                    document_hash=data.get("document_hash"),
                    document_version=data.get("document_version"),
                    span_type=data.get("span_type") or DEFAULT_SPAN_TYPE,
                    source_kind=data.get("source_kind") or DEFAULT_SOURCE_KIND,
                    source_uri=data.get("source_uri") or data.get("source_path"),
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
