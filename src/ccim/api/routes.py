"""`/v1/messages` 라우트. 요청을 미들웨어 체인에 태우고 SSE 또는 JSON으로 반환.

설계 §4.1 흐름:
    Agent → Gateway → PCFI → Compressor → ForwardAndIntercept → WriteRemap → Telemetry
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ccim.api.schemas import MessagesRequest

router = APIRouter(prefix="/v1", tags=["messages"])
logger = logging.getLogger(__name__)


@router.post("/messages", response_model=None)
async def create_message(
    request: MessagesRequest, http: Request
) -> StreamingResponse | JSONResponse:
    """Anthropic Messages API 호환 엔드포인트.

    - `stream=true`: Anthropic SSE 포맷 스트리밍 응답
    - `stream=false`: 단일 JSON 응답

    V1 스트리밍 정책: 내부적으로 `complete()` 후 SSE 합성 방출.
    (청크 단위 실시간 relay는 V2에서 retrieve_original 인터셉트 분리 후 활성화)
    """
    from ccim.middleware.chain import RequestContext, response_dict_to_sse

    # session_id 결정 순서:
    # 1) x-ccim-session 헤더 — 마커 안전 문자([A-Za-z0-9\-])만 허용, 위반 시 400
    # 2) CCIM_SESSION_PREFIX + UUID — prefix만 sanitize, UUID는 항상 안전
    import re as _re
    from ccim.config import get_settings
    _prefix = get_settings().session_prefix
    _raw_header = http.headers.get("x-ccim-session")
    if _raw_header:
        if not _re.fullmatch(r"[A-Za-z0-9\-]+", _raw_header):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_session_id",
                        "message": (
                            "x-ccim-session header must contain only "
                            "[A-Za-z0-9-] characters."
                        ),
                    }
                },
            )
        session_id: str = _raw_header
    else:
        # prefix sanitize — UUID 부분([0-9a-f-])은 항상 마커 안전
        _safe_prefix = _re.sub(r"[^A-Za-z0-9\-]", "-", _prefix) if _prefix else ""
        session_id = f"{_safe_prefix}{uuid.uuid4()}"

    ctx = RequestContext(session_id=session_id, request=request)

    chain = http.app.state.chain
    try:
        await chain.run(ctx)
    except httpx.HTTPStatusError as exc:
        import logging

        logging.getLogger(__name__).error("Chain HTTP error: %s", exc, exc_info=True)
        body = _http_status_error_body(exc)
        return JSONResponse(
            status_code=exc.response.status_code,
            content=body,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Chain error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "upstream_error", "message": str(exc)}},
        )

    # -- 차단 응답 ----------------------------------------------------------
    # ctx.response_json에 이미 error가 있으면 그대로 전달 (loop_limit 등).
    # 없으면 PCFI 기본 오류로 구성.
    if ctx.blocked:
        if ctx.response_json and "error" in ctx.response_json:
            return JSONResponse(
                status_code=ctx.block_status_code,
                content=ctx.response_json,
            )
        return JSONResponse(
            status_code=ctx.block_status_code,
            content={
                "error": {
                    "type": "pcfi_block",
                    "message": ctx.block_reason or "Request blocked by PCFI.",
                }
            },
        )

    # -- 응답 없음 (비정상) -------------------------------------------------
    if ctx.response_json is None:
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "upstream_error", "message": "No response from upstream LLM."}},
        )

    # -- 스트리밍 응답 -------------------------------------------------------
    if request.stream:
        response_data = ctx.response_json

        async def _sse_generator() -> AsyncIterator[bytes]:
            async for chunk in response_dict_to_sse(response_data):
                yield chunk

        return StreamingResponse(
            _sse_generator(),            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-CCIM-Session": session_id,
            },
        )

    # -- JSON
    return JSONResponse(
        content=ctx.response_json,
        headers={"X-CCIM-Session": session_id},
    )


@router.get("/models")
async def list_models(http: Request) -> JSONResponse:
    """List requestable models.

    Prefer upstream passthrough when an LLM client is initialized.
    Fall back to configured/static values so the route still works without lifespan.
    """
    llm_client = getattr(http.app.state, "llm_client", None)
    if llm_client is not None and hasattr(llm_client, "list_models"):
        try:
            payload = await llm_client.list_models()
        except httpx.HTTPStatusError as exc:
            logger.error("Models upstream HTTP error: %s", exc, exc_info=True)
            return JSONResponse(
                status_code=exc.response.status_code,
                content=_http_status_error_body(exc),
            )
        except Exception as exc:
            logger.warning("Models passthrough failed, using fallback: %s", exc)
        else:
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                return JSONResponse(content=payload)

    from ccim.config import get_settings

    return JSONResponse(content=_fallback_models_payload(get_settings()))


def _http_status_error_body(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    """Normalize upstream HTTP errors into the gateway error envelope."""
    try:
        payload = exc.response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None

    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload

    return {
        "error": {
            "type": "upstream_error",
            "message": str(exc),
        }
    }


def _fallback_models_payload(settings: Any) -> dict[str, Any]:
    """Fallback model listing when upstream discovery is unavailable."""
    if settings.llm_model:
        data = [{"id": settings.llm_model, "object": "model"}]
    elif settings.llm_provider == "anthropic":
        data = [
            {"id": "claude-opus-4-6", "object": "model"},
            {"id": "claude-sonnet-4-6", "object": "model"},
            {"id": "claude-haiku-4-5-20251001", "object": "model"},
        ]
    else:
        data = []

    return {"object": "list", "data": data}
