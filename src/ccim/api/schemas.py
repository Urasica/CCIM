"""Anthropic Messages API와 호환되는 Pydantic 모델.

V1은 Anthropic 형식만, OpenAI Chat Completions는 V1.x에서 추가.
필요한 필드만 정의 — `extra="allow"`로 모르는 필드는 통과.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[dict[str, Any]]
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Role
    content: str | list[ContentBlock]


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    description: str | None = None
    input_schema: dict[str, Any]


class MessagesRequest(BaseModel):
    """`POST /v1/messages` 요청 본문 (Anthropic 호환)."""

    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[Message]
    system: str | list[ContentBlock] | None = None
    max_tokens: int = Field(default=4096, ge=1)
    stream: bool = False
    tools: list[ToolDefinition] | None = None
    metadata: dict[str, Any] | None = None


class MessagesResponse(BaseModel):
    """`POST /v1/messages` 응답 본문 (non-streaming, 디버그/테스트용)."""

    model_config = ConfigDict(extra="allow")
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[ContentBlock]
    stop_reason: str | None = None
    usage: dict[str, int] | None = None


class ErrorResponse(BaseModel):
    error: dict[str, Any]
