"""Write Mapper unit tests.

Uses the same minimal _FakeRedis idea as test_reversibility but works on top of
ReversibilityStore so the integration path (store -> mapping -> translation)
is exercised.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ccim.reversibility.store import ContextRecord, ReversibilityStore
from ccim.write_mapper.mapper import (
    LINE_ARG_KEYS,
    WriteMapper,
    has_line_args,
    translate_line_with_mapping,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self.store[name] = value
        return True

    async def get(self, name: str):  # noqa: ANN201
        return self.store.get(name)

    async def delete(self, *names: str) -> int:
        n = 0
        for k in names:
            if k in self.store:
                del self.store[k]
                n += 1
        return n


# Sample mapping: a function body at original lines 6-10 was masked, marker on
# compressed line 5; lines after the function shift up by 5.
_SAMPLE_MAPPING = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 11, 7: 12, 8: 13}


async def _store_with_mapping(
    line_mapping: dict[int, int] | None = None,
    *,
    session_id: str = "s1",
    context_id: str = "001",
) -> ReversibilityStore:
    redis = _FakeRedis()
    store = ReversibilityStore(redis)
    if line_mapping is not None:
        await store.put(
            ContextRecord(
                session_id=session_id,
                context_id=context_id,
                original_code="<body>",
                language="python",
                line_mapping=line_mapping,
                created_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
            )
        )
    return store


# ----- pure helpers -----------------------------------------------------


def test_translate_line_with_mapping_hit() -> None:
    r = translate_line_with_mapping(_SAMPLE_MAPPING, 6)
    assert r.ok is True
    assert r.original_line == 11


def test_translate_line_with_mapping_miss() -> None:
    r = translate_line_with_mapping(_SAMPLE_MAPPING, 99)
    assert r.ok is False
    assert r.error and "not in mapping" in r.error


def test_has_line_args_known_tools() -> None:
    assert has_line_args("edit_file")
    assert has_line_args("apply_diff")
    assert not has_line_args("search_replace")
    assert not has_line_args("unknown_tool")


# ----- WriteMapper.translate_line --------------------------------------


async def test_translate_line_hit() -> None:
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    r = await mapper.translate_line(session_id="s1", context_id="001", compressed_line=6)
    assert r.ok and r.original_line == 11


async def test_translate_line_miss_returns_error() -> None:
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    r = await mapper.translate_line(session_id="s1", context_id="001", compressed_line=99)
    assert r.ok is False
    assert r.error


async def test_translate_line_no_mapping_in_store() -> None:
    store = await _store_with_mapping(None)
    mapper = WriteMapper(store)
    r = await mapper.translate_line(session_id="s1", context_id="404", compressed_line=1)
    assert r.ok is False
    assert "no mapping" in (r.error or "")


# ----- WriteMapper.remap_tool_use --------------------------------------


async def test_remap_edit_file_translates_all_line_keys() -> None:
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="edit_file",
        tool_input={
            "path": "foo.py",
            "line": 6,
            "start_line": 7,
            "end_line": 8,
            "new_text": "...",
        },
    )
    assert new_input["line"] == 11
    assert new_input["start_line"] == 12
    assert new_input["end_line"] == 13
    assert new_input["path"] == "foo.py"
    assert new_input["new_text"] == "..."
    assert all(r.ok for r in results)
    assert len(results) == 3


async def test_remap_apply_diff_with_partial_keys() -> None:
    """apply_diff might only carry `line` without start/end. Skip absent keys."""
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="apply_diff",
        tool_input={"line": 6, "diff": "@@ ..."},
    )
    assert new_input["line"] == 11
    assert len(results) == 1


async def test_remap_search_replace_passthrough() -> None:
    """search_replace has no line args; tool_input is unchanged."""
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    payload = {"path": "foo.py", "old": "a", "new": "b"}
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="search_replace",
        tool_input=payload,
    )
    assert new_input == payload
    assert results == []


async def test_remap_unknown_tool_passthrough() -> None:
    """Unknown tool names are passed through (Write Mapper is not a tool gatekeeper)."""
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    payload = {"foo": "bar"}
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="some_other_tool",
        tool_input=payload,
    )
    assert new_input == payload
    assert results == []


async def test_remap_with_no_mapping_blocks_edit() -> None:
    """When mapping is missing, return original input + error MapResult."""
    store = await _store_with_mapping(None)
    mapper = WriteMapper(store)
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="404",
        tool_name="edit_file",
        tool_input={"line": 6},
    )
    assert new_input == {"line": 6}  # untouched
    assert len(results) == 1
    assert results[0].ok is False
    assert "no mapping" in (results[0].error or "")


async def test_remap_miss_keeps_original_value_and_reports_error() -> None:
    """Per-key miss: keep original int, surface error in results list."""
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="edit_file",
        tool_input={"line": 6, "start_line": 999},
    )
    assert new_input["line"] == 11
    assert new_input["start_line"] == 999  # un-translatable, left as-is
    assert any(r.ok is False for r in results)


async def test_remap_non_int_line_arg_reports_type_error() -> None:
    store = await _store_with_mapping(_SAMPLE_MAPPING)
    mapper = WriteMapper(store)
    new_input, results = await mapper.remap_tool_use(
        session_id="s1",
        context_id="001",
        tool_name="edit_file",
        tool_input={"line": "6"},  # string, not int
    )
    assert new_input["line"] == "6"  # untouched
    assert results and results[0].ok is False
    assert "expected int" in (results[0].error or "")


def test_line_arg_keys_table_has_required_tools() -> None:
    """Regression: ensure edit_file and apply_diff stay covered."""
    assert "edit_file" in LINE_ARG_KEYS
    assert "apply_diff" in LINE_ARG_KEYS
    assert "line" in LINE_ARG_KEYS["edit_file"]
