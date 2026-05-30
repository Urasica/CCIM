"""Anthropic <-> OpenAI request / response / SSE translation.

Internal representation is Anthropic Messages API (matches api.schemas).
- Anthropic upstream: passthrough.
- OpenAI / OpenAI-compatible upstream (vLLM, Ollama, LM Studio): translate at the boundary.

V1 streaming policy: text-only Anthropic SSE is emitted from OpenAI streams.
Tool_calls during streaming are buffered and emitted as a single content_block at end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ccim.api.schemas import (
    Message,
    MessagesRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

ANTHROPIC_VERSION = "2023-06-01"


# ----- Request: Anthropic -> OpenAI -----------------------------------


def anthropic_to_openai_request(req: MessagesRequest, *, stream: bool) -> dict[str, Any]:
    """Convert MessagesRequest -> OpenAI Chat Completions request body."""
    messages: list[dict[str, Any]] = []

    # System: prepend
    if req.system is not None:
        sys_text = _flatten_system(req.system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for m in req.messages:
        messages.extend(_message_to_openai(m))

    body: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "stream": stream,
    }
    body[_openai_max_tokens_field(req.model)] = req.max_tokens

    if req.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in req.tools
        ]
    return body


def _openai_max_tokens_field(model: str) -> str:
    """Return the token-limit field expected by the target OpenAI model."""
    normalized = model.lower()
    if normalized.startswith("gpt-5"):
        return "max_completion_tokens"
    return "max_tokens"


def _flatten_system(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for b in system:
            if isinstance(b, TextBlock):
                parts.append(b.text)
            elif isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
        return "\n".join(parts)
    return str(system)


def _message_to_openai(m: Message) -> list[dict[str, Any]]:
    """One Anthropic message -> one or more OpenAI messages.

    Anthropic blocks tool_result inside a user message; OpenAI splits these into
    role='tool' messages with tool_call_id. We emit them in original order.
    """
    if isinstance(m.content, str):
        return [{"role": m.role, "content": m.content}]

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in m.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    },
                }
            )
        elif isinstance(block, ToolResultBlock):
            content = block.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": content,
                }
            )

    out: list[dict[str, Any]] = []
    # OpenAI requires tool messages to immediately follow the assistant tool_call message.
    # For user-role messages that carry both tool results and text, emit tool results first.
    if m.role == "user":
        out.extend(tool_results)
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})
    else:
        # assistant role: tool_calls stay inside the same message; no tool_results expected
        msg: dict[str, Any] = {
            "role": m.role,
            "content": "\n".join(text_parts) if text_parts else "",
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)
        out.extend(tool_results)  # should be empty for assistant, but handle defensively
    return out


# ----- Response: OpenAI -> Anthropic ----------------------------------


_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def openai_to_anthropic_response(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Convert OpenAI Chat Completions response -> Anthropic Messages response."""
    choices = resp.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message") or {}

    content_blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        if tc.get("type") not in (None, "function"):
            continue
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments", "") or "{}")
        except json.JSONDecodeError:
            args = {"_raw_arguments": fn.get("arguments", "")}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": args,
            }
        )

    finish = choice.get("finish_reason")
    stop_reason = _FINISH_TO_STOP.get(finish or "stop", "end_turn")

    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }


# ----- Streaming: OpenAI SSE -> Anthropic SSE -------------------------


def encode_sse_event(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


async def openai_sse_to_anthropic(
    raw_byte_stream: AsyncIterator[bytes], *, model: str
) -> AsyncIterator[bytes]:
    """Translate raw OpenAI SSE bytes -> Anthropic-shape SSE bytes.

    V1 limitation: tool_call streaming is not split into structured Anthropic
    content_block events. Tool calls accumulate and are emitted once at the end
    as a `content_block_start` -> `input_json_delta` -> `content_block_stop` group.
    """
    started = False
    text_block_open = False
    output_tokens = 0
    msg_id = ""
    pending_tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    buf = ""

    async for raw in raw_byte_stream:
        buf += raw.decode("utf-8", errors="replace")
        # SSE events are separated by blank lines.
        while "\n\n" in buf:
            event_block, _, buf = buf.partition("\n\n")
            for line in event_block.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    async for ev in _close_anthropic_stream(
                        started=started,
                        text_block_open=text_block_open,
                        pending_tool_calls=pending_tool_calls,
                        finish_reason=finish_reason or "stop",
                        output_tokens=output_tokens,
                    ):
                        yield ev
                    return
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if not msg_id:
                    msg_id = chunk.get("id", "")

                if not started:
                    started = True
                    yield encode_sse_event(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": msg_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": model,
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            },
                        },
                    )

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch0 = choices[0]
                delta = ch0.get("delta") or {}

                # Text delta
                content = delta.get("content")
                if isinstance(content, str) and content:
                    if not text_block_open:
                        text_block_open = True
                        yield encode_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    output_tokens += 1
                    yield encode_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": content},
                        },
                    )

                # Tool calls (buffered for V1)
                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    cur = pending_tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc_delta.get("id"):
                        cur["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        cur["name"] = fn["name"]
                    if fn.get("arguments"):
                        cur["arguments"] += fn["arguments"]

                if ch0.get("finish_reason"):
                    finish_reason = ch0["finish_reason"]

    # Stream ended without [DONE]
    async for ev in _close_anthropic_stream(
        started=started,
        text_block_open=text_block_open,
        pending_tool_calls=pending_tool_calls,
        finish_reason=finish_reason or "stop",
        output_tokens=output_tokens,
    ):
        yield ev


async def _close_anthropic_stream(
    *,
    started: bool,
    text_block_open: bool,
    pending_tool_calls: dict[int, dict[str, Any]],
    finish_reason: str,
    output_tokens: int,
) -> AsyncIterator[bytes]:
    if not started:
        return
    if text_block_open:
        yield encode_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        )

    # Emit each buffered tool call as a single block (text-only streaming limitation).
    next_index = 1 if text_block_open else 0
    for _, tc in sorted(pending_tool_calls.items()):
        try:
            input_obj = json.loads(tc["arguments"]) if tc["arguments"] else {}
        except json.JSONDecodeError:
            input_obj = {"_raw_arguments": tc["arguments"]}
        yield encode_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": next_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": {},
                },
            },
        )
        yield encode_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": next_index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(input_obj, ensure_ascii=False),
                },
            },
        )
        yield encode_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": next_index},
        )
        next_index += 1

    yield encode_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _FINISH_TO_STOP.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": output_tokens},
        },
    )
    yield encode_sse_event("message_stop", {"type": "message_stop"})
