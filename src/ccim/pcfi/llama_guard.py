"""Llama Guard 3 client (Ollama / LM Studio compatible chat endpoint).

V1: prefer 8B quantized; fallback to 1B if memory-constrained (design section 6).

Llama Guard response format:
    safe
    or
    unsafe
    S6,S11
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class GuardResult:
    safe: bool
    categories: list[str]
    raw_response: str


class GuardClient(Protocol):
    """Minimal interface PCFIEnforcer depends on. Tests substitute a stub."""

    async def classify(self, conversation: str) -> GuardResult: ...

    async def aclose(self) -> None: ...


class LlamaGuardClient:
    """Calls Ollama-compatible /api/chat to invoke Llama Guard."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def classify(self, conversation: str) -> GuardResult:
        resp = await self._client.post(
            f"{self._base_url}/api/chat",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": conversation}],
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data.get("message") or {}).get("content", "").strip()
        return _parse_guard_output(text)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_guard_output(text: str) -> GuardResult:
    """Parse 'safe' / 'unsafe\\nS6,S11'. Empty or unknown -> conservatively unsafe."""
    if not text:
        return GuardResult(safe=False, categories=["UNKNOWN"], raw_response="")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return GuardResult(safe=False, categories=["UNKNOWN"], raw_response=text)
    head = lines[0].lower()
    if head.startswith("safe"):
        return GuardResult(safe=True, categories=[], raw_response=text)
    if head.startswith("unsafe"):
        cats: list[str] = []
        if len(lines) > 1:
            cats = [c.strip() for c in lines[1].split(",") if c.strip()]
        return GuardResult(safe=False, categories=cats or ["UNSPECIFIED"], raw_response=text)
    return GuardResult(safe=False, categories=["UNKNOWN"], raw_response=text)
