"""LLM에게 주입할 `retrieve_original` 도구 정의 + 시스템 프롬프트 힌트.

설계 §3.2.3 + §6 위험: LLM이 환각하지 않고 도구를 부르도록 강하게 유도.
"""

from __future__ import annotations

from typing import Any

# Anthropic 도구 정의 형식. OpenAI는 V1.x에서 변환기 추가.
RETRIEVE_ORIGINAL_TOOL: dict[str, Any] = {
    "name": "retrieve_original",
    "description": (
        "MUST CALL when you encounter a marker shaped like `<<CTX_xxx:N>>` in the "
        "conversation. This marker represents code whose body has been masked for "
        "context efficiency. To read or edit the original code, call this tool — "
        "DO NOT guess or fabricate the contents."
    ),
    "input_schema": {
        "type": "object",
        "required": ["context_id"],
        "properties": {
            "context_id": {
                "type": "string",
                "description": "Full context id from the marker, e.g. 'sessionA:001'",
            }
        },
    },
}


def build_system_hint() -> str:
    """시스템 프롬프트에 합쳐 넣을 한 단락. 도구 호출 의무를 명시."""
    return (
        "Some code blocks in this conversation have been compressed and replaced "
        "with markers of the form `<<CTX_session:N>>`. Whenever you need to read, "
        "reason about, or edit such code, you MUST first call the `retrieve_original` "
        "tool with the full context id. Never invent or paraphrase the masked body."
    )
