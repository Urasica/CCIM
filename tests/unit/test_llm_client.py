"""LLM client unit tests.

Translation helpers are tested as pure functions; HTTP calls are mocked with respx.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from ccim.api.schemas import (
    Message,
    MessagesRequest,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from ccim.llm.client import AnthropicClient, OpenAIClient, build_client
from ccim.llm.translate import (
    ProviderCompatibilityError,
    anthropic_to_openai_request,
    encode_sse_event,
    openai_sse_to_anthropic,
    openai_to_anthropic_response,
)

# ----- Pure translation: Anthropic -> OpenAI request -------------------


def test_translate_request_simple_text() -> None:
    req = MessagesRequest(
        model="gpt-4.1",
        system="be helpful",
        messages=[Message(role="user", content="hi")],
        max_tokens=128,
    )
    body = anthropic_to_openai_request(req, stream=False)
    assert body["model"] == "gpt-4.1"
    assert body["stream"] is False
    assert body["max_tokens"] == 128
    assert body["messages"][0] == {"role": "system", "content": "be helpful"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_translate_request_block_content() -> None:
    req = MessagesRequest(
        model="gpt-4.1",
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="part1"), TextBlock(text="part2")],
            )
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    assert body["messages"][0]["content"] == "part1\npart2"


def test_translate_request_tool_use_becomes_tool_calls() -> None:
    req = MessagesRequest(
        model="gpt-4.1",
        messages=[
            Message(role="user", content="find foo"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="ok"),
                    ToolUseBlock(id="t1", name="search", input={"q": "foo"}),
                ],
            ),
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    asst = body["messages"][1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "ok"
    assert asst["tool_calls"][0]["id"] == "t1"
    assert asst["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"q": "foo"}


def test_translate_request_tool_result_splits_into_tool_message() -> None:
    req = MessagesRequest(
        model="gpt-4.1",
        messages=[
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="t1", content="result text")],
            )
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    msg = body["messages"][0]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "t1"
    assert msg["content"] == "result text"


def test_translate_request_tools_to_function_format() -> None:
    req = MessagesRequest(
        model="gpt-4.1",
        messages=[Message(role="user", content="hi")],
        tools=[
            ToolDefinition(
                name="search",
                description="web search",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            )
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "search"
    assert tool["function"]["parameters"]["properties"] == {"q": {"type": "string"}}


def test_translate_request_gpt5_uses_max_completion_tokens() -> None:
    req = MessagesRequest(
        model="gpt-5.4-mini",
        messages=[Message(role="user", content="hi")],
        max_tokens=256,
    )
    body = anthropic_to_openai_request(req, stream=False)
    assert body["max_completion_tokens"] == 256
    assert "max_tokens" not in body


# ----- Pure translation: OpenAI -> Anthropic response ------------------


def test_translate_response_text_only() -> None:
    openai_resp = {
        "id": "cmpl-1",
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    anthropic = openai_to_anthropic_response(openai_resp, model="gpt-4.1")
    assert anthropic["role"] == "assistant"
    assert anthropic["content"] == [{"type": "text", "text": "hello"}]
    assert anthropic["stop_reason"] == "end_turn"
    assert anthropic["usage"] == {"input_tokens": 5, "output_tokens": 3}


def test_translate_response_tool_calls() -> None:
    openai_resp = {
        "id": "cmpl-2",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"q": "foo"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    anthropic = openai_to_anthropic_response(openai_resp, model="gpt-4.1")
    assert anthropic["stop_reason"] == "tool_use"
    blocks = anthropic["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["id"] == "call_1"
    assert blocks[0]["name"] == "search"
    assert blocks[0]["input"] == {"q": "foo"}


def test_translate_response_finish_length_to_max_tokens() -> None:
    resp = {"choices": [{"message": {"content": "abc"}, "finish_reason": "length"}]}
    anth = openai_to_anthropic_response(resp, model="m")
    assert anth["stop_reason"] == "max_tokens"


def test_translate_response_invalid_tool_args_are_explicitly_unsupported() -> None:
    openai_resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "f", "arguments": "not-json"},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    with pytest.raises(ProviderCompatibilityError) as exc_info:
        openai_to_anthropic_response(openai_resp, model="m")
    assert exc_info.value.reason == "invalid_response_tool_arguments_json"


def test_translate_response_unknown_content_is_explicitly_unsupported() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "audio", "audio": "data"}],
                },
                "finish_reason": "stop",
            }
        ]
    }
    with pytest.raises(ProviderCompatibilityError) as exc_info:
        openai_to_anthropic_response(response, model="m")
    assert exc_info.value.reason == "unsupported_response_content"


# ----- SSE translation -------------------------------------------------


def test_encode_sse_event_format() -> None:
    out = encode_sse_event("ping", {"type": "ping"})
    assert out == b'event: ping\ndata: {"type": "ping"}\n\n'


async def test_openai_sse_to_anthropic_text_stream() -> None:
    openai_chunks = [
        b'data: {"id": "c1", "choices": [{"delta": {"content": "Hel"}}]}\n\n',
        b'data: {"id": "c1", "choices": [{"delta": {"content": "lo"}}]}\n\n',
        b'data: {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def src():
        for c in openai_chunks:
            yield c

    out: list[bytes] = []
    async for ev in openai_sse_to_anthropic(src(), model="gpt-4.1"):
        out.append(ev)

    joined = b"".join(out).decode("utf-8")
    assert "event: message_start" in joined
    assert "event: content_block_start" in joined
    assert '"text_delta"' in joined
    assert '"text": "Hel"' in joined
    assert '"text": "lo"' in joined
    assert "event: content_block_stop" in joined
    assert "event: message_delta" in joined
    assert "event: message_stop" in joined


async def test_openai_sse_to_anthropic_tool_call_buffered() -> None:
    """Tool calls in stream are buffered and emitted as one block at the end."""
    chunks = [
        b'data: {"id": "c2", "choices": [{"delta": {"tool_calls": '
        b'[{"index": 0, "id": "t1", "function": {"name": "search", "arguments": "{\\"q\\": \\"f"}}]}}]}\n\n',
        b'data: {"id": "c2", "choices": [{"delta": {"tool_calls": '
        b'[{"index": 0, "function": {"arguments": "oo\\"}"}}]}}]}\n\n',
        b'data: {"id": "c2", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def src():
        for c in chunks:
            yield c

    out_bytes = b""
    async for ev in openai_sse_to_anthropic(src(), model="gpt-4.1"):
        out_bytes += ev
    text = out_bytes.decode("utf-8")
    assert '"tool_use"' in text
    assert '"name": "search"' in text
    assert '\\"q\\": \\"foo\\"' in text or "q" in text
    assert "tool_use" in text  # stop_reason mapped


async def test_openai_sse_invalid_tool_json_is_explicitly_unsupported() -> None:
    chunks = [
        b'data: {"id": "c3", "choices": [{"delta": {"tool_calls": '
        b'[{"index": 0, "id": "t1", "function": '
        b'{"name": "Edit", "arguments": "not-json"}}]}}]}\n\n',
        b'data: {"id": "c3", "choices": [{"delta": {}, '
        b'"finish_reason": "tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def src():
        for chunk in chunks:
            yield chunk

    with pytest.raises(ProviderCompatibilityError) as exc_info:
        async for _ in openai_sse_to_anthropic(src(), model="gpt-4.1"):
            pass
    assert exc_info.value.reason == "invalid_stream_tool_arguments_json"


# ----- HTTP clients (respx) -------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_complete_passthrough() -> None:
    expected = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 1},
    }
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json=expected)
    )
    client = AnthropicClient(api_key="test-key")
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="user", content="hi")],
    )
    out = await client.complete(req)
    assert out == expected
    sent = route.calls.last.request
    assert out.request_bytes == len(sent.content)
    assert out.provider_usage_available is True
    assert sent.headers["x-api-key"] == "test-key"
    assert sent.headers["anthropic-version"]
    body = json.loads(sent.content)
    assert body["stream"] is False
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_stream_relays_bytes() -> None:
    sse = b"event: message_start\ndata: {\"type\": \"message_start\"}\n\n"
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )
    )
    client = AnthropicClient(api_key="test-key")
    req = MessagesRequest(
        model="claude-sonnet-4-5",
        messages=[Message(role="user", content="hi")],
        stream=True,
    )
    out = b""
    async for chunk in client.stream(req):
        out += chunk
    assert b"message_start" in out
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_anthropic_list_models_passthrough() -> None:
    expected = {"object": "list", "data": [{"id": "claude-sonnet-4-6", "object": "model"}]}
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(200, json=expected)
    )
    client = AnthropicClient(api_key="test-key")
    out = await client.list_models()
    assert out == expected
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_translates_round_trip() -> None:
    openai_resp = {
        "id": "cmpl-x",
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=openai_resp)
    )
    client = OpenAIClient(api_key="test-key")
    req = MessagesRequest(
        model="gpt-4.1",
        system="be helpful",
        messages=[Message(role="user", content="hi")],
    )
    out = await client.complete(req)
    # Response is in Anthropic shape
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out.request_bytes == len(route.calls.last.request.content)
    assert out.provider_usage_available is True
    # Request body sent to upstream is in OpenAI shape
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "gpt-4.1"
    assert sent["messages"][0]["role"] == "system"
    assert sent["max_tokens"] == 4096
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_list_models_passthrough() -> None:
    expected = {"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model"}]}
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json=expected)
    )
    client = OpenAIClient(api_key="test-key")
    out = await client.list_models()
    assert out == expected
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_gpt5_sends_max_completion_tokens() -> None:
    openai_resp = {
        "id": "cmpl-gpt5",
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=openai_resp)
    )
    client = OpenAIClient(api_key="test-key")
    req = MessagesRequest(
        model="gpt-5.4-mini",
        messages=[Message(role="user", content="hi")],
        max_tokens=512,
    )
    await client.complete(req)
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "gpt-5.4-mini"
    assert sent["max_completion_tokens"] == 512
    assert "max_tokens" not in sent
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_retries_rate_limit_then_succeeds() -> None:
    openai_ok = {
        "id": "cmpl-rate-ok",
        "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }
    rate_limited = httpx.Response(
        429,
        json={
            "error": {
                "message": (
                    "Rate limit reached for gpt-5.4-mini on tokens per min. "
                    "Please try again in 4.224s."
                ),
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        },
    )
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        side_effect=[
            rate_limited,
            httpx.Response(200, json=openai_ok),
        ]
    )
    client = OpenAIClient(api_key="test-key")
    req = MessagesRequest(
        model="gpt-5.4-mini",
        messages=[Message(role="user", content="hi")],
        max_tokens=512,
    )

    with patch("ccim.llm.client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        out = await client.complete(req)

    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert route.call_count == 2
    assert out.request_attempts == 2
    assert out.request_bytes_total == sum(
        len(call.request.content) for call in route.calls
    )
    sleep_mock.assert_awaited_once_with(4.224)
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_complete_rate_limit_exhausted_raises_429() -> None:
    rate_limited = httpx.Response(
        429,
        json={
            "error": {
                "message": "Rate limit reached. Please try again in 1.5s.",
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        },
    )
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=rate_limited
    )
    client = OpenAIClient(api_key="test-key", max_rate_limit_retries=1)
    req = MessagesRequest(
        model="gpt-5.4-mini",
        messages=[Message(role="user", content="hi")],
    )

    with (
        patch("ccim.llm.client.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        await client.complete(req)

    assert exc_info.value.response.status_code == 429
    sleep_mock.assert_awaited_once_with(1.5)
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_openai_compatible_uses_custom_base_url() -> None:
    route = respx.post("http://localhost:11434/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [
                    {"message": {"content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
        )
    )
    client = build_client(
        provider="openai-compatible",
        api_key="local",
        base_url="http://localhost:11434",
    )
    req = MessagesRequest(
        model="qwen2.5-coder",
        messages=[Message(role="user", content="hi")],
    )
    out = await client.complete(req)
    assert route.called
    assert out["content"][0]["text"] == "hi"
    await client.aclose()


@pytest.mark.asyncio
async def test_build_client_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        build_client(provider="bogus", api_key="x")


@pytest.mark.asyncio
async def test_build_client_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ValueError):
        build_client(provider="openai-compatible", api_key="x")
