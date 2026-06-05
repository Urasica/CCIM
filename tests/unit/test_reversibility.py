"""Reversibility unit tests.

ReversibilityStore is exercised against a tiny in-memory FakeRedis that
implements just the SET/GET/DELETE methods we use; a real-Redis
roundtrip lives under tests/integration/.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ccim.compress.markers import build_marker, parse_marker
from ccim.reversibility.interceptor import (
    RETRIEVE_TOOL_NAME,
    ReversibilityInterceptor,
)
from ccim.reversibility.retrieve_tool import RETRIEVE_ORIGINAL_TOOL, build_system_hint
from ccim.reversibility.store import ContextRecord, ReversibilityStore, ToolResultRecord

# ----- FakeRedis --------------------------------------------------------


class _FakeRedis:
    """Bare-minimum stand-in: SET (with ex), GET, DELETE. No TTL expiry simulation."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.last_ex: int | None = None

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self.store[name] = value
        self.last_ex = ex
        return True

    async def get(self, name: str):
        return self.store.get(name)

    async def delete(self, *names: str) -> int:
        n = 0
        for k in names:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


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
        source_path="tools/compare/large_reference.py",
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
    assert got.source_path == "tools/compare/large_reference.py"
    assert got.symbol_name == "transform_batch_001"
    assert got.original_lines == (10, 11)


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
