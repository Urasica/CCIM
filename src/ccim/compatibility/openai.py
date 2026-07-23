"""Strict OpenAI Chat Completions ingress and egress adapters.

The middleware chain continues to use ``MessagesRequest`` as its canonical
request model. Unsupported input is rejected at this boundary instead of being
silently dropped.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ccim.api.schemas import (
    Message,
    MessagesRequest,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

_SUPPORTED_TOP_LEVEL = {
    "model",
    "messages",
    "max_tokens",
    "max_completion_tokens",
    "stream",
    "tools",
    "temperature",
    "top_p",
    "stop",
    "metadata",
}
_STOP_REASON_TO_OPENAI = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}


class CompatibilityError(ValueError):
    """Stable compatibility rejection used by API adapters."""

    def __init__(
        self,
        *,
        code: str,
        reason: str,
        path: str,
        message: str,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.path = path
        self.status_code = status_code

    def as_error(self) -> dict[str, Any]:
        return {
            "error": {
                "message": str(self),
                "type": "unsupported_schema",
                "param": self.path,
                "code": self.code,
                "ccim_reason": self.reason,
            }
        }


def _reject(
    reason: str,
    path: str,
    message: str,
    *,
    code: str = "CCIM_UNSUPPORTED_SCHEMA",
) -> CompatibilityError:
    return CompatibilityError(code=code, reason=reason, path=path, message=message)


def openai_chat_to_messages(body: dict[str, Any]) -> MessagesRequest:
    """Convert an OpenAI Chat Completions request to the canonical request."""
    if not isinstance(body, dict):
        raise _reject("request_not_object", "$", "Request body must be a JSON object.")

    unsupported = sorted(set(body) - _SUPPORTED_TOP_LEVEL)
    if unsupported:
        name = unsupported[0]
        raise _reject(
            "unsupported_request_field",
            name,
            f"OpenAI Chat Completions field {name!r} is not supported.",
        )

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _reject("missing_model", "model", "model must be a non-empty string.")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _reject(
            "invalid_messages",
            "messages",
            "messages must be a non-empty array.",
        )

    system_parts: list[str] = []
    messages: list[Message] = []
    saw_non_system = False
    for index, raw_message in enumerate(raw_messages):
        path = f"messages[{index}]"
        if not isinstance(raw_message, dict):
            raise _reject("message_not_object", path, f"{path} must be an object.")
        role = raw_message.get("role")
        if role in {"system", "developer"}:
            if saw_non_system:
                raise _reject(
                    "system_message_out_of_order",
                    f"{path}.role",
                    "system and developer messages must precede conversation messages.",
                )
            system_parts.append(_text_content(raw_message.get("content"), f"{path}.content"))
            _reject_message_extras(raw_message, path, {"role", "content"})
            continue

        saw_non_system = True
        if role == "user":
            _reject_message_extras(raw_message, path, {"role", "content"})
            messages.append(
                Message(
                    role="user",
                    content=_text_content(raw_message.get("content"), f"{path}.content"),
                )
            )
            continue

        if role == "assistant":
            _reject_message_extras(
                raw_message,
                path,
                {"role", "content", "tool_calls"},
            )
            blocks: list[TextBlock | ToolUseBlock] = []
            content = raw_message.get("content")
            if content is not None:
                text = _text_content(content, f"{path}.content")
                if text:
                    blocks.append(TextBlock(text=text))
            blocks.extend(_tool_calls(raw_message.get("tool_calls"), path))
            if not blocks:
                blocks.append(TextBlock(text=""))
            messages.append(Message(role="assistant", content=blocks))
            continue

        if role == "tool":
            _reject_message_extras(
                raw_message,
                path,
                {"role", "content", "tool_call_id"},
            )
            tool_call_id = raw_message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise _reject(
                    "invalid_tool_call_id",
                    f"{path}.tool_call_id",
                    "tool_call_id must be a non-empty string.",
                )
            messages.append(
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id=tool_call_id,
                            content=_text_content(
                                raw_message.get("content"),
                                f"{path}.content",
                            ),
                        )
                    ],
                )
            )
            continue

        raise _reject(
            "unsupported_message_role",
            f"{path}.role",
            f"Message role {role!r} is not supported.",
        )

    if (
        "max_completion_tokens" in body
        and "max_tokens" in body
        and body["max_completion_tokens"] != body["max_tokens"]
    ):
        raise _reject(
            "ambiguous_max_tokens",
            "max_completion_tokens",
            "max_tokens and max_completion_tokens must match when both are present.",
        )
    raw_max_tokens = body.get("max_completion_tokens", body.get("max_tokens", 4096))
    if not isinstance(raw_max_tokens, int) or isinstance(raw_max_tokens, bool):
        raise _reject(
            "invalid_max_tokens",
            "max_completion_tokens",
            "max_tokens must be a positive integer.",
        )
    if raw_max_tokens < 1:
        raise _reject(
            "invalid_max_tokens",
            "max_completion_tokens",
            "max_tokens must be a positive integer.",
        )

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise _reject("invalid_stream", "stream", "stream must be a boolean.")

    request_data: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "system": "\n\n".join(system_parts) if system_parts else None,
        "max_tokens": raw_max_tokens,
        "stream": stream,
        "tools": _tools(body.get("tools")),
    }
    for field in ("temperature", "top_p"):
        value = body.get(field)
        if value is not None:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise _reject(
                    f"invalid_{field}",
                    field,
                    f"{field} must be a number.",
                )
            request_data[field] = value

    stop = body.get("stop")
    if stop is not None:
        stop_sequences = [stop] if isinstance(stop, str) else stop
        if (
            not isinstance(stop_sequences, list)
            or not stop_sequences
            or not all(isinstance(item, str) and item for item in stop_sequences)
        ):
            raise _reject(
                "invalid_stop",
                "stop",
                "stop must be a non-empty string or array of non-empty strings.",
            )
        request_data["stop_sequences"] = stop_sequences

    metadata = body.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise _reject("invalid_metadata", "metadata", "metadata must be an object.")
        request_data["metadata"] = metadata

    return MessagesRequest.model_validate(request_data)


def _reject_message_extras(
    message: dict[str, Any],
    path: str,
    allowed: set[str],
) -> None:
    extras = sorted(set(message) - allowed)
    if extras:
        name = extras[0]
        raise _reject(
            "unsupported_message_field",
            f"{path}.{name}",
            f"Message field {name!r} is not supported.",
        )


def _text_content(value: Any, path: str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise _reject(
            "unsupported_content",
            path,
            "Content must be a string or an array of text blocks.",
        )
    parts: list[str] = []
    for index, block in enumerate(value):
        block_path = f"{path}[{index}]"
        if not isinstance(block, dict) or block.get("type") != "text":
            block_type = block.get("type") if isinstance(block, dict) else None
            raise _reject(
                "unsupported_content_block",
                block_path,
                f"Content block type {block_type!r} is not supported.",
            )
        if set(block) - {"type", "text"}:
            raise _reject(
                "unsupported_content_block_field",
                block_path,
                "Text content blocks may contain only type and text.",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise _reject(
                "invalid_text_content",
                f"{block_path}.text",
                "Text block text must be a string.",
            )
        parts.append(text)
    return "\n".join(parts)


def _tool_calls(value: Any, message_path: str) -> list[ToolUseBlock]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _reject(
            "invalid_tool_calls",
            f"{message_path}.tool_calls",
            "tool_calls must be an array.",
        )
    blocks: list[ToolUseBlock] = []
    for index, call in enumerate(value):
        path = f"{message_path}.tool_calls[{index}]"
        if not isinstance(call, dict) or call.get("type") != "function":
            raise _reject(
                "unsupported_tool_call_type",
                path,
                "Only function tool calls are supported.",
            )
        if set(call) - {"id", "type", "function"}:
            raise _reject(
                "unsupported_tool_call_field",
                path,
                "Tool calls may contain only id, type, and function.",
            )
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise _reject(
                "invalid_tool_call_id",
                f"{path}.id",
                "Tool call id must be a non-empty string.",
            )
        if not isinstance(function, dict):
            raise _reject(
                "invalid_tool_function",
                f"{path}.function",
                "Tool call function must be an object.",
            )
        if set(function) - {"name", "arguments"}:
            raise _reject(
                "unsupported_tool_function_field",
                f"{path}.function",
                "Tool function may contain only name and arguments.",
            )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise _reject(
                "invalid_tool_name",
                f"{path}.function.name",
                "Tool name must be a non-empty string.",
            )
        arguments = _function_arguments(
            function.get("arguments"),
            f"{path}.function.arguments",
        )
        blocks.append(ToolUseBlock(id=call_id, name=name, input=arguments))
    return blocks


def _function_arguments(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise _reject(
            "invalid_tool_arguments",
            path,
            "Tool arguments must be a JSON object or encoded JSON object.",
        )
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise _reject(
            "invalid_tool_arguments_json",
            path,
            "Tool arguments must contain valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise _reject(
            "invalid_tool_arguments",
            path,
            "Decoded tool arguments must be an object.",
        )
    return parsed


def _tools(value: Any) -> list[ToolDefinition] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise _reject("invalid_tools", "tools", "tools must be an array.")
    tools: list[ToolDefinition] = []
    for index, raw_tool in enumerate(value):
        path = f"tools[{index}]"
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            raise _reject(
                "unsupported_tool_definition_type",
                path,
                "Only function tool definitions are supported.",
            )
        if set(raw_tool) - {"type", "function"}:
            raise _reject(
                "unsupported_tool_definition_field",
                path,
                "Tool definitions may contain only type and function.",
            )
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            raise _reject(
                "invalid_tool_definition",
                f"{path}.function",
                "Tool function definition must be an object.",
            )
        if set(function) - {"name", "description", "parameters"}:
            raise _reject(
                "unsupported_tool_function_field",
                f"{path}.function",
                "Tool function definitions support name, description, and parameters.",
            )
        name = function.get("name")
        parameters = function.get("parameters")
        description = function.get("description")
        if not isinstance(name, str) or not name:
            raise _reject(
                "invalid_tool_name",
                f"{path}.function.name",
                "Tool name must be a non-empty string.",
            )
        if not isinstance(parameters, dict):
            raise _reject(
                "invalid_tool_parameters",
                f"{path}.function.parameters",
                "Tool parameters must be a JSON schema object.",
            )
        if description is not None and not isinstance(description, str):
            raise _reject(
                "invalid_tool_description",
                f"{path}.function.description",
                "Tool description must be a string.",
            )
        tools.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema=parameters,
            )
        )
    return tools


def messages_to_openai_response(
    response: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    """Convert the canonical response to OpenAI Chat Completions JSON."""
    message, finish_reason = _openai_message_and_finish(response)
    usage = response.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    return {
        "id": response.get("id", ""),
        "object": "chat.completion",
        "created": int(response.get("created", 0) or 0),
        "model": response.get("model") or requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _openai_message_and_finish(
    response: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    content = response.get("content")
    if not isinstance(content, list):
        raise _reject(
            "invalid_canonical_content",
            "response.content",
            "Canonical response content must be an array.",
        )
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        path = f"response.content[{index}]"
        if not isinstance(block, dict):
            raise _reject("invalid_canonical_block", path, "Content block must be an object.")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise _reject(
                    "invalid_canonical_text",
                    f"{path}.text",
                    "Text block text must be a string.",
                )
            text_parts.append(text)
            continue
        if block_type == "tool_use":
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                raise _reject(
                    "invalid_canonical_tool_input",
                    f"{path}.input",
                    "Tool input must be an object.",
                )
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                    },
                }
            )
            continue
        raise _reject(
            "unsupported_canonical_content_block",
            path,
            f"Canonical content block type {block_type!r} is not supported.",
        )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    stop_reason = response.get("stop_reason") or "end_turn"
    finish_reason = _STOP_REASON_TO_OPENAI.get(stop_reason)
    if finish_reason is None:
        raise _reject(
            "unsupported_stop_reason",
            "response.stop_reason",
            f"Canonical stop reason {stop_reason!r} is not supported.",
        )
    return message, finish_reason


async def messages_to_openai_sse(
    response: dict[str, Any],
    *,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Emit a synthesized OpenAI-compatible SSE stream from a complete response."""
    converted = messages_to_openai_response(response, requested_model=requested_model)
    choice = converted["choices"][0]
    message = choice["message"]
    base = {
        "id": converted["id"],
        "object": "chat.completion.chunk",
        "created": converted["created"],
        "model": converted["model"],
    }
    yield _openai_sse_chunk(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }
            ],
        }
    )
    if message.get("content"):
        yield _openai_sse_chunk(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": message["content"]},
                        "finish_reason": None,
                    }
                ],
            }
        )
    for index, tool_call in enumerate(message.get("tool_calls") or []):
        yield _openai_sse_chunk(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    **tool_call,
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
    yield _openai_sse_chunk(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": choice["finish_reason"],
                }
            ],
            "usage": converted["usage"],
        }
    )
    yield b"data: [DONE]\n\n"


def _openai_sse_chunk(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
