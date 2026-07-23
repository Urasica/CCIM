"""Deterministic ingress, provider, and write compatibility contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccim.api.schemas import MessagesRequest
from ccim.compatibility.openai import (
    CompatibilityError,
    messages_to_openai_response,
    messages_to_openai_sse,
    openai_chat_to_messages,
)
from ccim.compatibility.write_tools import inspect_write_tool
from ccim.llm.translate import ProviderCompatibilityError
from ccim.middleware.chain import (
    CompatibilityValidationMiddleware,
    CompressMiddleware,
    CurrentTurnWriteGuardMiddleware,
    ForwardAndInterceptMiddleware,
    MiddlewareChain,
    RequestContext,
)

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "compatibility_matrix.json"


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def test_openai_and_anthropic_ingress_share_canonical_fixture() -> None:
    case = _fixture()["canonical_equivalence"]
    anthropic = MessagesRequest.model_validate(case["anthropic"])
    openai = openai_chat_to_messages(case["openai"])

    assert openai.model_dump(mode="json", exclude_none=True) == anthropic.model_dump(
        mode="json",
        exclude_none=True,
    )


async def test_two_ingress_paths_produce_same_compression_metadata() -> None:
    from ccim.compress.ast_compressor import ASTCompressor

    case = _fixture()["canonical_equivalence"]
    requests = [
        MessagesRequest.model_validate(case["anthropic"]),
        openai_chat_to_messages(case["openai"]),
    ]

    class _Settings:
        compression_trigger_tokens = 10
        compression_target_tokens = 5
        redis_ttl_seconds = 3600
        compression_enable_retrieve = True
        current_turn_compression_enabled = True
        current_turn_compression_trigger_tokens = 10
        current_turn_compression_read_tools = "Read"
        compression_write_guard_enabled = True
        compression_write_guard_tools = "Edit,MultiEdit,Write"
        compression_cluster_summary_enabled = False

    class _Store:
        def __init__(self) -> None:
            self.records: list[Any] = []

        async def put(self, record: Any) -> None:
            self.records.append(record)

    results: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    for request in requests:
        store = _Store()
        middleware = CompressMiddleware(
            compressor=ASTCompressor(),
            store=store,
            settings=_Settings(),
        )
        ctx = RequestContext(session_id="compat-session", request=request)
        await MiddlewareChain(stages=[middleware]).run(ctx)
        flags = ctx.extras["feature_flags"]
        results.append(
            (
                {
                    key: flags[key]
                    for key in (
                        "compress_any",
                        "compress_candidates",
                        "compress_current_turn_candidates",
                        "compress_current_turn_contexts",
                        "compress_context_ids",
                    )
                },
                json.loads(
                    re.sub(
                        r"<<CTX_compat-session:[A-Za-z0-9\-_]+>>",
                        "<<CTX_NORMALIZED>>",
                        json.dumps(
                            ctx.request.model_dump(mode="json", exclude_none=True),
                            ensure_ascii=False,
                        ),
                    )
                ),
                sorted(ctx.extras["current_turn_context_sources"].values()),
            )
        )

    assert results[0] == results[1]
    assert results[0][0]["compress_any"] is True
    assert results[0][0]["compress_current_turn_contexts"] > 0


def test_openai_ingress_rejects_unknown_content_block() -> None:
    body = {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "x"}}],
            }
        ],
    }
    with pytest.raises(CompatibilityError) as exc_info:
        openai_chat_to_messages(body)
    assert exc_info.value.reason == "unsupported_content_block"
    assert exc_info.value.path == "messages[0].content[0]"


def test_openai_response_and_sse_preserve_tools_and_usage() -> None:
    canonical = {
        "id": "msg-1",
        "model": "gpt-4.1",
        "content": [
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "Read",
                "input": {"file_path": "src/example.py"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }
    response = messages_to_openai_response(canonical, requested_model="gpt-4.1")
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert response["usage"]["total_tokens"] == 16
    assert json.loads(
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    ) == {"file_path": "src/example.py"}


async def test_openai_synthesized_sse_ends_with_done() -> None:
    canonical = {
        "id": "msg-1",
        "model": "gpt-4.1",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    chunks = [
        chunk
        async for chunk in messages_to_openai_sse(
            canonical,
            requested_model="gpt-4.1",
        )
    ]
    text = b"".join(chunks).decode()
    assert '"content": "ok"' in text
    assert text.endswith("data: [DONE]\n\n")


@pytest.mark.parametrize("case", _fixture()["write_tools"], ids=lambda case: case["case_id"])
def test_write_tool_fixture_matrix(case: dict[str, Any]) -> None:
    inspection = inspect_write_tool(case["name"], case["input"])
    assert inspection is not None
    assert inspection.status == case["expected_status"]
    if inspection.status == "supported":
        assert inspection.canonical_name == case["expected_canonical"]
    else:
        assert inspection.reason == case["expected_reason"]


async def test_unsupported_write_schema_is_blocked_with_telemetry_reason() -> None:
    ctx = RequestContext(
        session_id="compat-session",
        request=MessagesRequest.model_validate(
            _fixture()["canonical_equivalence"]["anthropic"]
        ),
    )
    ctx.response_json = {
        "id": "msg-1",
        "model": "m",
        "content": [
            {
                "type": "tool_use",
                "id": "patch-1",
                "name": "apply_patch",
                "input": {"patch": "*** Begin Patch"},
            }
        ],
        "stop_reason": "tool_use",
    }
    await MiddlewareChain(stages=[CompatibilityValidationMiddleware()]).run(ctx)

    flags = ctx.extras["feature_flags"]
    assert ctx.response_json["content"][0]["type"] == "text"
    assert flags["write_compatibility_status"] == "unsupported"
    assert flags["write_compatibility_reason"] == "unsupported_write_tool"


async def test_edit_file_alias_uses_same_retrieval_guard() -> None:
    class _Settings:
        compression_write_guard_enabled = True
        compression_write_guard_tools = "Edit,MultiEdit,Write"

    ctx = RequestContext(
        session_id="compat-session",
        request=MessagesRequest(
            model="gpt-4.1",
            messages=[],
        ),
    )
    ctx.extras["current_turn_context_ids"] = ["compat-session:001"]
    ctx.extras["current_turn_source_paths"] = {"src/example.py"}
    ctx.extras["current_turn_context_sources"] = {
        "compat-session:001": "src/example.py"
    }
    ctx.extras["retrieved_contexts"] = {
        "compat-session:001": "def f():\n    return 1\n"
    }
    ctx.response_json = {
        "id": "msg-edit",
        "model": "gpt-4.1",
        "content": [
            {
                "type": "tool_use",
                "id": "edit-1",
                "name": "edit_file",
                "input": {
                    "path": "src/example.py",
                    "old_string": "    return 1",
                    "new_string": "    return 2",
                },
            }
        ],
        "stop_reason": "tool_use",
    }
    await MiddlewareChain(
        stages=[
            CompatibilityValidationMiddleware(),
            CurrentTurnWriteGuardMiddleware(_Settings()),
        ]
    ).run(ctx)

    assert ctx.response_json["content"][0]["type"] == "tool_use"
    flags = ctx.extras["feature_flags"]
    assert flags["write_compatibility_canonical_tool"] == "edit"
    assert flags["current_turn_write_guard_blocked"] is False
    assert flags["current_turn_write_guard_mode"] == "allowed_after_retrieve"


async def test_provider_schema_error_reaches_structured_flags() -> None:
    client = MagicMock()
    client.complete = AsyncMock(
        side_effect=ProviderCompatibilityError(
            reason="invalid_response_tool_arguments_json",
            path="choices[0].message.tool_calls[0].function.arguments",
            message="invalid tool arguments",
        )
    )
    interceptor = MagicMock()
    ctx = RequestContext(
        session_id="compat-session",
        request=MessagesRequest(
            model="gpt-4.1",
            messages=[],
        ),
    )
    await MiddlewareChain(
        stages=[
            ForwardAndInterceptMiddleware(client, interceptor),
            CompatibilityValidationMiddleware(),
        ]
    ).run(ctx)

    assert ctx.blocked is True
    assert ctx.response_json["error"]["type"] == "unsupported_provider_schema"
    assert (
        ctx.extras["feature_flags"]["provider_compatibility_reason"]
        == "invalid_response_tool_arguments_json"
    )
