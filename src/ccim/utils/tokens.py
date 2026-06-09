"""토큰 추정 — tiktoken cl100k_base를 fallback 인코더로 사용.

Anthropic이 응답 메타데이터에서 정확값을 돌려주므로, 본 추정기는
- trigger 휴리스틱 (`should_compress`)에서 임계치 비교
- 압축 전후 절감량 빠른 추정 (텔레메트리)
용도. 정확값이 필요하면 응답의 `usage`로 보정.
"""

from __future__ import annotations

import json
from functools import lru_cache

import tiktoken

from ccim.api.schemas import (
    Message,
    MessagesRequest,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

# Anthropic Messages API의 메시지 1건당 평균 메타 오버헤드 (role 마커 등).
# 정확한 값은 모델별로 다르나 ~3-5 토큰 수준이라 4로 고정.
_OVERHEAD_PER_MESSAGE = 4
_OVERHEAD_PER_TOOL_USE = 8


@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def estimate_text_tokens(text: str) -> int:
    """대략적 토큰 수. 빈 문자열은 0."""
    if not text:
        return 0
    return len(_encoder().encode(text, disallowed_special=()))


def estimate_message_tokens(message: Message) -> int:
    """메시지 한 건 토큰. content가 list면 각 블록 합산 + 메시지 메타 오버헤드."""
    total = _OVERHEAD_PER_MESSAGE
    if isinstance(message.content, str):
        return total + estimate_text_tokens(message.content)
    for block in message.content:
        if isinstance(block, TextBlock):
            total += estimate_text_tokens(block.text)
        elif isinstance(block, ToolUseBlock):
            total += _OVERHEAD_PER_TOOL_USE
            total += estimate_text_tokens(block.name)
            total += estimate_text_tokens(json.dumps(block.input, ensure_ascii=False))
        elif isinstance(block, ToolResultBlock):
            content = block.content
            if isinstance(content, str):
                total += estimate_text_tokens(content)
            else:
                total += estimate_text_tokens(json.dumps(content, ensure_ascii=False))
    return total


def estimate_tool_definition_tokens(tool: ToolDefinition) -> int:
    """Approximate tokens contributed by a single tool schema."""
    total = _OVERHEAD_PER_MESSAGE
    total += estimate_text_tokens(tool.name)
    total += estimate_text_tokens(tool.description or "")
    total += estimate_text_tokens(json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True))
    return total


def estimate_request_tokens(request: MessagesRequest) -> int:
    """Approximate total input tokens for an Anthropic-style request."""
    total = sum(estimate_message_tokens(m) for m in request.messages)
    if request.system is not None:
        total += estimate_message_tokens(
            Message(role="system", content=request.system)
        )
    if request.tools:
        total += sum(estimate_tool_definition_tokens(t) for t in request.tools)
    return total
