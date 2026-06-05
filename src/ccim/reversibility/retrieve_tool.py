"""retrieve_original tool definition and system prompt hint."""

from __future__ import annotations

from typing import Any

RETRIEVE_ORIGINAL_TOOL: dict[str, Any] = {
    "name": "retrieve_original",
    "description": (
        "MUST CALL when you encounter a marker shaped like `<<CTX_xxx:N>>` in the "
        "conversation. This marker represents code whose body has been masked for "
        "context efficiency. To read or edit the original code, call this tool. "
        "If multiple markers are needed, pass all of them in `context_ids` in one "
        "call. DO NOT guess or fabricate the contents."
    ),
    "input_schema": {
        "type": "object",
        "oneOf": [
            {"required": ["context_id"]},
            {"required": ["context_ids"]},
        ],
        "properties": {
            "context_id": {
                "type": "string",
                "description": "Full context id from the marker, e.g. 'sessionA:001'",
            },
            "context_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Full context ids from multiple markers, e.g. "
                    "['sessionA:001', 'sessionA:002']"
                ),
            },
        },
    },
}


def build_system_hint() -> str:
    """Return the system hint that tells the model how to restore markers."""
    return (
        "Some code blocks in this conversation have been compressed and replaced "
        "with markers of the form `<<CTX_session:N>>`. Whenever you need to read, "
        "reason about, or edit such code, you MUST first call the `retrieve_original` "
        "tool with the full context id. If you need multiple markers, use a single "
        "`retrieve_original` call with `context_ids`. Never invent or paraphrase "
        "the masked body."
    )
