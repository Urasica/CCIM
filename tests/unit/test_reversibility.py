"""Reversibility unit tests.

ReversibilityStore is exercised against a tiny in-memory FakeRedis that
implements just the SET/GET/DELETE methods we use; a real-Redis
roundtrip lives under tests/integration/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ccim.compress.markers import build_marker, parse_marker
from ccim.reversibility.evidence_guard import EvidenceGuard, EvidenceGuardRequest
from ccim.reversibility.interceptor import (
    RETRIEVE_TOOL_NAME,
    ReversibilityInterceptor,
)
from ccim.reversibility.persistent import SQLiteEvidenceStore
from ccim.reversibility.retrieve_tool import RETRIEVE_ORIGINAL_TOOL, build_system_hint
from ccim.reversibility.store import (
    ContextRecord,
    EvidenceIdentity,
    EvidenceSpan,
    ReversibilityStore,
    ToolResultRecord,
    compute_document_hash,
)

# ----- FakeRedis --------------------------------------------------------


class _FakeRedis:
    """Bare-minimum stand-in: SET (with ex), GET, DELETE. No TTL expiry simulation."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self.last_ex: int | None = None

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self.store[name] = value
        self.last_ex = ex
        if ex is not None:
            self.ttls[name] = ex
        return True

    async def get(self, name: str):
        return self.store.get(name)

    async def delete(self, *names: str) -> int:
        n = 0
        for k in names:
            if k in self.store:
                del self.store[k]
                n += 1
            self.sets.pop(k, None)
            self.ttls.pop(k, None)
        return n

    async def sadd(self, name: str, *values: str) -> int:
        bucket = self.sets.setdefault(name, set())
        before = len(bucket)
        bucket.update(values)
        return len(bucket) - before

    async def srem(self, name: str, *values: str) -> int:
        bucket = self.sets.setdefault(name, set())
        before = len(bucket)
        for value in values:
            bucket.discard(value)
        return before - len(bucket)

    async def smembers(self, name: str) -> set[str]:
        return self.sets.get(name, set())

    async def expire(self, name: str, time: int) -> bool:
        self.ttls[name] = time
        return True

    async def ttl(self, name: str) -> int:
        return self.ttls.get(name, -2 if name not in self.store and name not in self.sets else -1)

    async def memory_usage(self, name: str) -> int | None:
        value = self.store.get(name)
        if value is None:
            return None
        if isinstance(value, bytes):
            return len(value)
        return len(value.encode("utf-8"))


def _record(session_id: str = "s1", context_id: str = "001") -> ContextRecord:
    return ContextRecord(
        session_id=session_id,
        context_id=context_id,
        original_code="def f():\n    return 1\n",
        language="python",
        line_mapping={1: 1, 2: 2, 3: 3},
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
    )


def _metadata_record() -> ContextRecord:
    return ContextRecord(
        session_id="s1",
        context_id="meta001",
        original_code="    value = 1\n    return value\n",
        language="python",
        line_mapping={3: 10},
        source_path="tests/compare/large_reference.py",
        symbol_name="transform_batch_001",
        original_lines=(10, 11),
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
    )


# ----- Markers (sanity, also covered in compressor tests) ---------------


def test_marker_roundtrip() -> None:
    m = build_marker("sessionA", "001")
    parsed = parse_marker(m)
    assert parsed is not None
    assert parsed.session_id == "sessionA"
    assert parsed.context_id == "001"
    assert parsed.full_id == "sessionA:001"


# ----- Store ------------------------------------------------------------


async def test_store_put_writes_with_ttl() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    await store.put(_record())
    assert "ctx:s1:001" in redis.store
    assert redis.last_ex == 300
    assert redis.sets["idx:ctx:s1"] == {"001"}
    assert redis.ttls["idx:ctx:s1"] == 300


async def test_store_get_returns_record() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    await store.put(_record())
    got = await store.get("s1", "001")
    assert got is not None
    assert got.original_code == "def f():\n    return 1\n"
    assert got.language == "python"
    assert got.line_mapping == {1: 1, 2: 2, 3: 3}


async def test_store_get_returns_context_metadata() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    await store.put(_metadata_record())
    got = await store.get("s1", "meta001")
    assert got is not None
    assert got.source_path == "tests/compare/large_reference.py"
    assert got.symbol_name == "transform_batch_001"
    assert got.original_lines == (10, 11)


async def test_store_roundtrips_evidence_metadata() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    record = ContextRecord(
        session_id="s1",
        context_id="span001",
        original_code="2026-06-10T10:00:00Z ERROR payment timeout\n",
        language="text",
        line_mapping={},
        document_id="incident-log",
        document_hash=compute_document_hash("line\r\n"),
        document_version=2,
        span_type="log_window",
        source_kind="log",
        source_uri="logs/app.log",
        original_lines=(120, 121),
        metadata={"level": "ERROR"},
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )

    await store.put(record)
    got = await store.get("s1", "span001")

    assert got is not None
    assert got.document_id == "incident-log"
    assert got.document_version == 2
    assert got.span_type == "log_window"
    assert got.source_kind == "log"
    assert got.source_uri == "logs/app.log"
    assert got.metadata == {"level": "ERROR"}


async def test_store_list_contexts_includes_evidence_metadata() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    await store.put(
        ContextRecord(
            session_id="s1",
            context_id="doc001",
            original_code="## Policy\nCancel before Friday.\n",
            language="text",
            line_mapping={},
            document_id="policy",
            document_hash=compute_document_hash("policy"),
            document_version=3,
            span_type="document_section",
            source_kind="document",
            source_uri="policy.md",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
    )

    entry = (await store.list_contexts("s1"))[0]

    assert entry.document_id == "policy"
    assert entry.document_version == 3
    assert entry.span_type == "document_section"
    assert entry.source_kind == "document"
    assert entry.source_uri == "policy.md"


def test_compute_document_hash_normalizes_newlines() -> None:
    assert compute_document_hash("a\r\nb\r") == compute_document_hash("a\nb\n")


def test_evidence_identity_validates_version() -> None:
    try:
        EvidenceIdentity(document_id="doc", document_hash="abc", document_version=0)
    except ValueError as exc:
        assert "document_version" in str(exc)
    else:
        raise AssertionError("expected invalid version to raise")


def test_evidence_span_converts_to_context_record() -> None:
    span = EvidenceSpan(
        session_id="s1",
        document_id="thread",
        document_hash=compute_document_hash("hello"),
        document_version=1,
        span_id="email001",
        span_type="email_message",
        source_kind="email",
        source_uri="thread.eml",
        original_text="hello",
        line_start=5,
        line_end=6,
        metadata={"sender": "a@example.com"},
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )

    record = span.to_context_record()
    roundtrip = record.to_evidence_span()

    assert record.context_id == "email001"
    assert record.original_code == "hello"
    assert record.language == "text"
    assert record.original_lines == (5, 6)
    assert roundtrip.full_context_id == "s1:email001"
    assert roundtrip.source_kind == "email"
    assert roundtrip.metadata == {"sender": "a@example.com"}


async def test_store_get_miss_returns_none() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    assert await store.get("s1", "404") is None


async def test_store_delete() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record())
    await store.delete("s1", "001")
    assert await store.get("s1", "001") is None
    assert redis.sets["idx:ctx:s1"] == set()


async def test_store_list_contexts_returns_operational_metadata() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300)
    await store.put(_metadata_record())

    contexts = await store.list_contexts("s1")

    assert len(contexts) == 1
    entry = contexts[0]
    assert entry.session_id == "s1"
    assert entry.context_id == "meta001"
    assert entry.redis_key == "ctx:s1:meta001"
    assert entry.language == "python"
    assert entry.source_path == "tests/compare/large_reference.py"
    assert entry.symbol_name == "transform_batch_001"
    assert entry.original_lines == (10, 11)
    assert entry.original_chars == len("    value = 1\n    return value\n")
    assert entry.ttl_seconds == 300
    assert entry.memory_bytes_est is not None


async def test_store_get_line_mapping_only() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record())
    mapping = await store.get_line_mapping("s1", "001")
    assert mapping == {1: 1, 2: 2, 3: 3}


async def test_store_tool_result_roundtrip() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    record = ToolResultRecord(
        session_id="s1",
        content_hash="abc123",
        content="long command output",
        metadata={"lines": 1},
    )
    await store.put_tool_result(record)
    got = await store.get_tool_result("s1", "abc123")
    assert got is not None
    assert got.content == "long command output"
    assert got.metadata == {"lines": 1}


async def test_sqlite_evidence_store_roundtrip(tmp_path: Path) -> None:
    persistent = SQLiteEvidenceStore(tmp_path / "evidence.sqlite")
    record = ContextRecord(
        session_id="s1",
        context_id="log001",
        original_code="ERROR failed\n",
        language="text",
        line_mapping={},
        document_id="log",
        document_hash=compute_document_hash("ERROR failed\n"),
        document_version=1,
        span_type="log_window",
        source_kind="log",
        source_uri="app.log",
        metadata={"component": "api"},
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )

    await persistent.put_context(record)
    got = await persistent.get_context("s1", "log001")
    by_hash = await persistent.get_contexts_by_document_hash(record.document_hash or "")

    assert got is not None
    assert got.original_code == "ERROR failed\n"
    assert got.source_kind == "log"
    assert got.metadata == {"component": "api"}
    assert [item.context_id for item in by_hash] == ["log001"]


async def test_store_get_warms_redis_from_persistent(tmp_path: Path) -> None:
    persistent = SQLiteEvidenceStore(tmp_path / "evidence.sqlite")
    record = ContextRecord(
        session_id="s1",
        context_id="doc001",
        original_code="Important paragraph\n",
        language="text",
        line_mapping={},
        document_id="doc",
        document_hash=compute_document_hash("Important paragraph\n"),
        document_version=1,
        span_type="document_section",
        source_kind="document",
        source_uri="doc.md",
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    await persistent.put_context(record)

    redis = _FakeRedis()
    store = ReversibilityStore(redis, ttl_seconds=300, persistent_store=persistent)
    got = await store.get("s1", "doc001")

    assert got is not None
    assert got.original_code == "Important paragraph\n"
    assert "ctx:s1:doc001" in redis.store
    assert store.stats.redis_misses == 1
    assert store.stats.persistent_hits == 1
    assert store.stats.redis_warm_loads == 1


async def test_store_handles_bytes_response() -> None:
    """Real redis-py can return bytes; ReversibilityStore must decode."""
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record())
    # Manually replace the str value with bytes to mimic redis-py behavior.
    raw = redis.store["ctx:s1:001"].encode("utf-8")
    redis.store["ctx:s1:001"] = raw  # type: ignore[assignment]
    got = await store.get("s1", "001")
    assert got is not None
    assert got.original_code == "def f():\n    return 1\n"


# ----- Evidence Guard ---------------------------------------------------


async def test_evidence_guard_blocks_before_retrieve() -> None:
    guard = EvidenceGuard()

    decision = await guard.evaluate(
        EvidenceGuardRequest(
            action_type="reply_draft",
            required_context_ids=["s1:001"],
            expected_session_id="s1",
        ),
        retrieved_contexts={},
    )

    assert decision.allowed is False
    assert decision.reason == "blocked_no_retrieve"
    assert decision.missing_context_ids == ["s1:001"]
    flags = decision.to_feature_flags()
    assert flags["evidence_guard_blocked"] is True
    assert flags["evidence_guard_action_type"] == "reply_draft"
    assert flags["evidence_guard_block_reason"] == "blocked_no_retrieve"


async def test_evidence_guard_allows_after_retrieve() -> None:
    guard = EvidenceGuard()

    decision = await guard.evaluate(
        EvidenceGuardRequest(
            action_type="evidence_packet",
            required_context_ids=["s1:001"],
            expected_session_id="s1",
        ),
        retrieved_contexts={"s1:001": "original evidence text"},
    )

    assert decision.allowed is True
    assert decision.reason == "allowed_after_retrieve"
    assert decision.validated_context_ids == ["s1:001"]
    assert decision.to_feature_flags()["evidence_guard_blocked"] is False


async def test_evidence_guard_blocks_version_mismatch() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(
        ContextRecord(
            session_id="s1",
            context_id="doc001",
            original_code="Current policy paragraph\n",
            language="text",
            line_mapping={},
            document_id="policy",
            document_hash=compute_document_hash("Current policy paragraph\n"),
            document_version=2,
            span_type="document_section",
            source_kind="document",
            source_uri="policy.md",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
    )
    guard = EvidenceGuard(store)

    decision = await guard.evaluate(
        EvidenceGuardRequest(
            action_type="final_claim",
            required_context_ids=["s1:doc001"],
            expected_session_id="s1",
            expected_document_versions={"policy": 1},
        ),
        retrieved_contexts={"s1:doc001": "Current policy paragraph\n"},
    )

    assert decision.allowed is False
    assert decision.reason == "blocked_version_mismatch"
    assert decision.records_checked == 1
    assert len(decision.version_mismatches) == 1
    mismatch = decision.version_mismatches[0]
    assert mismatch.document_id == "policy"
    assert mismatch.expected_version == 1
    assert mismatch.actual_version == 2


async def test_evidence_guard_blocks_cross_session_context() -> None:
    guard = EvidenceGuard()

    decision = await guard.evaluate(
        EvidenceGuardRequest(
            action_type="ticket_comment",
            required_context_ids=["s2:001"],
            expected_session_id="s1",
        ),
        retrieved_contexts={"s2:001": "wrong session evidence"},
    )

    assert decision.allowed is False
    assert decision.reason == "blocked_cross_session_context"
    assert decision.missing_context_ids == ["s2:001"]


# ----- Interceptor ------------------------------------------------------


async def test_interceptor_hits_returns_original_code() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record())
    interceptor = ReversibilityInterceptor(store)
    res = await interceptor.handle_tool_use({"context_id": "s1:001"})
    assert res.is_error is False
    assert res.content == "def f():\n    return 1\n"
    assert interceptor.stats.retrieve_calls == 1
    assert interceptor.stats.retrieve_hits == 1
    assert interceptor.stats.retrieve_misses == 0


async def test_interceptor_context_ids_returns_grouped_originals() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record(context_id="001"))
    await store.put(
        ContextRecord(
            session_id="s1",
            context_id="002",
            original_code="def g():\n    return 2\n",
            language="python",
            line_mapping={1: 1, 2: 2},
            created_at=datetime(2026, 4, 28, tzinfo=UTC),
        )
    )
    interceptor = ReversibilityInterceptor(store)
    res = await interceptor.handle_tool_use(
        {"context_ids": ["s1:001", "s1:002"]},
        expected_session_id="s1",
    )

    assert res.is_error is False
    assert "## s1:001" in res.content
    assert "def f()" in res.content
    assert "## s1:002" in res.content
    assert "def g()" in res.content
    assert interceptor.stats.retrieve_calls == 1
    assert interceptor.stats.retrieve_hits == 2
    assert interceptor.stats.retrieve_misses == 0


async def test_interceptor_miss_returns_error_text() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    interceptor = ReversibilityInterceptor(store)
    res = await interceptor.handle_tool_use({"context_id": "s1:404"})
    assert res.is_error is True
    assert "not found" in res.content
    assert interceptor.stats.retrieve_misses == 1


async def test_interceptor_invalid_format_is_error() -> None:
    redis = _FakeRedis()
    interceptor = ReversibilityInterceptor(ReversibilityStore(redis))
    res = await interceptor.handle_tool_use({"context_id": "no-colon"})
    assert res.is_error is True
    assert "invalid" in res.content


async def test_interceptor_missing_field_is_error() -> None:
    redis = _FakeRedis()
    interceptor = ReversibilityInterceptor(ReversibilityStore(redis))
    res = await interceptor.handle_tool_use({})  # no context_id at all
    assert res.is_error is True


async def test_interceptor_rejects_cross_session_context() -> None:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    await store.put(_record(session_id="s-other"))
    interceptor = ReversibilityInterceptor(store)
    res = await interceptor.handle_tool_use(
        {"context_id": "s-other:001"},
        expected_session_id="s1",
    )
    assert res.is_error is True
    assert "different session" in res.content
    assert interceptor.stats.retrieve_calls == 1
    assert interceptor.stats.retrieve_hits == 0
    assert interceptor.stats.retrieve_misses == 1


def test_is_retrieve_call() -> None:
    assert ReversibilityInterceptor.is_retrieve_call(RETRIEVE_TOOL_NAME) is True
    assert ReversibilityInterceptor.is_retrieve_call("other_tool") is False


# ----- Tool definition --------------------------------------------------


def test_retrieve_tool_definition_shape() -> None:
    assert RETRIEVE_ORIGINAL_TOOL["name"] == "retrieve_original"
    assert "evidence span" in RETRIEVE_ORIGINAL_TOOL["description"]
    schema = RETRIEVE_ORIGINAL_TOOL["input_schema"]
    assert schema["type"] == "object"
    assert {"required": ["context_id"]} in schema["oneOf"]
    assert {"required": ["context_ids"]} in schema["oneOf"]
    assert "context_id" in schema["properties"]
    assert "context_ids" in schema["properties"]


def test_build_system_hint_mentions_marker_format() -> None:
    hint = build_system_hint()
    assert "<<CTX_" in hint
    assert "retrieve_original" in hint
    assert "evidence" in hint
