"""Local OpenAI-compatible upstream used for wire-size measurement tests."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI

from ccim.utils.tokens import estimate_text_tokens

app = FastAPI(title="CCIM local measurement upstream")


@app.post("/v1/chat/completions")
async def complete(body: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "id": f"mock-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": body.get("model", "ccim-local-measurement"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Local measurement response.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": estimate_text_tokens(serialized),
            "completion_tokens": 4,
            "total_tokens": estimate_text_tokens(serialized) + 4,
        },
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "ccim-local-measurement", "object": "model"}],
    }
