"""Upstream LLM clients (Anthropic, OpenAI / OpenAI-compatible).

Internal payload is always a `MessagesRequest` (Anthropic Messages shape).
- AnthropicClient: passthrough.
- OpenAIClient: translates request and response at the boundary; works for
  vanilla OpenAI, vLLM, Ollama, LM Studio (just point base_url at the right host).

Both expose:
    complete(request) -> dict        # Anthropic-shape JSON
    stream(request) -> AsyncIterator[bytes]  # Anthropic-shape SSE bytes
    aclose()
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from ccim.api.schemas import MessagesRequest
from ccim.llm.translate import (
    ANTHROPIC_VERSION,
    anthropic_to_openai_request,
    openai_sse_to_anthropic,
    openai_to_anthropic_response,
)

logger = logging.getLogger(__name__)

_RETRY_AFTER_TEXT_RE = re.compile(r"Please try again in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)


class LLMClient(Protocol):
    """Common interface used by the gateway middleware chain."""

    name: str

    async def complete(self, request: MessagesRequest) -> dict[str, Any]: ...

    async def stream(self, request: MessagesRequest) -> AsyncIterator[bytes]: ...

    async def list_models(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class AnthropicClient:
    """Passes Anthropic Messages API through unchanged."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        *,
        timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def complete(self, request: MessagesRequest) -> dict[str, Any]:
        body = request.model_dump(exclude_none=True, mode="json")
        body["stream"] = False
        resp = await self._client.post(
            f"{self._base_url}/v1/messages", headers=self._headers(), json=body
        )
        resp.raise_for_status()
        return resp.json()

    async def stream(self, request: MessagesRequest) -> AsyncIterator[bytes]:
        body = request.model_dump(exclude_none=True, mode="json")
        body["stream"] = True
        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/messages",
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def list_models(self) -> dict[str, Any]:
        resp = await self._client.get(f"{self._base_url}/v1/models", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class OpenAIClient:
    """Translates Anthropic-shape requests to OpenAI Chat Completions."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com",
        *,
        timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        max_rate_limit_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._extra_headers = extra_headers or {}
        self._max_rate_limit_retries = max(0, max_rate_limit_retries)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._extra_headers)
        return headers

    async def complete(self, request: MessagesRequest) -> dict[str, Any]:
        body = anthropic_to_openai_request(request, stream=False)
        _cap_openai_output_tokens(body)
        logger.debug(
            "OpenAI request model=%s max_tokens=%s max_completion_tokens=%s",
            body.get("model"),
            body.get("max_tokens"),
            body.get("max_completion_tokens"),
        )
        last_resp: httpx.Response | None = None
        for attempt in range(self._max_rate_limit_retries + 1):
            resp = await self._client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=body,
            )
            last_resp = resp
            if not resp.is_error:
                return openai_to_anthropic_response(resp.json(), model=request.model)

            logger.error("OpenAI %s error body: %s", resp.status_code, resp.text[:500])
            if resp.status_code != 429 or attempt >= self._max_rate_limit_retries:
                resp.raise_for_status()

            retry_after_s = _openai_retry_after_seconds(resp)
            logger.warning(
                "OpenAI rate limit hit for model=%s; retrying in %.3fs (attempt %d/%d)",
                body.get("model"),
                retry_after_s,
                attempt + 1,
                self._max_rate_limit_retries,
            )
            await asyncio.sleep(retry_after_s)

        assert last_resp is not None
        last_resp.raise_for_status()
        raise AssertionError("unreachable")

    async def stream(self, request: MessagesRequest) -> AsyncIterator[bytes]:
        body = anthropic_to_openai_request(request, stream=True)
        _cap_openai_output_tokens(body)
        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/chat/completions",
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()

            async def _raw() -> AsyncIterator[bytes]:
                async for chunk in resp.aiter_bytes():
                    yield chunk

            async for ev in openai_sse_to_anthropic(_raw(), model=request.model):
                yield ev

    async def list_models(self) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base_url}/v1/models",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _cap_openai_output_tokens(body: dict[str, Any]) -> None:
    """Clamp output-token limits for models/endpoints with lower hard caps."""
    for field in ("max_tokens", "max_completion_tokens"):
        if field in body and body[field] > 16384:
            body[field] = 16384


def _openai_retry_after_seconds(resp: httpx.Response) -> float:
    """Best-effort retry delay for OpenAI 429 responses."""
    retry_after_ms = resp.headers.get("retry-after-ms")
    if retry_after_ms:
        try:
            return max(0.0, float(retry_after_ms) / 1000.0)
        except ValueError:
            pass

    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass

    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if isinstance(message, str):
                match = _RETRY_AFTER_TEXT_RE.search(message)
                if match:
                    try:
                        return max(0.0, float(match.group(1)))
                    except ValueError:
                        pass

    return 1.0


def build_client(
    *,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    timeout_s: float = 120.0,
) -> LLMClient:
    """Build a client by provider name.

    Recognized providers:
      - "anthropic" -> AnthropicClient
      - "openai" -> OpenAIClient
      - "openai-compatible" -> OpenAIClient with a custom base_url
    """
    if provider == "anthropic":
        return AnthropicClient(
            api_key=api_key,
            base_url=base_url or "https://api.anthropic.com",
            timeout_s=timeout_s,
        )
    if provider == "openai":
        return OpenAIClient(
            api_key=api_key,
            base_url=base_url or "https://api.openai.com",
            timeout_s=timeout_s,
        )
    if provider == "openai-compatible":
        if not base_url:
            raise ValueError("openai-compatible provider requires base_url")
        return OpenAIClient(api_key=api_key, base_url=base_url, timeout_s=timeout_s)
    raise ValueError(f"unknown provider: {provider!r}")
