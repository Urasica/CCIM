"""`/v1/messages` 라우트. 요청을 미들웨어 체인에 태우고 SSE 또는 JSON으로 반환.

설계 §4.1 흐름:
    Agent → Gateway → PCFI → Compressor → ForwardAndIntercept → WriteRemap → Telemetry
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ccim.api.schemas import MessagesRequest

router = APIRouter(prefix="/v1", tags=["messages"])
logger = logging.getLogger(__name__)


@router.post("/messages", response_model=None)
async def create_message(
    http: Request,
    body: Annotated[Any, Body()],
) -> StreamingResponse | JSONResponse:
    """Anthropic Messages API 호환 엔드포인트.

    - `stream=true`: Anthropic SSE 포맷 스트리밍 응답
    - `stream=false`: 단일 JSON 응답

    V1 스트리밍 정책: 내부적으로 `complete()` 후 SSE 합성 방출.
    (청크 단위 실시간 relay는 V2에서 retrieve_original 인터셉트 분리 후 활성화)
    """
    from ccim.middleware.chain import RequestContext, response_dict_to_sse

    session_id, session_error = _resolve_session_id(http)
    if session_error is not None:
        return session_error
    assert session_id is not None
    try:
        request = MessagesRequest.model_validate(body)
    except ValidationError:
        reason, path = _anthropic_validation_reason(body)
        model = body.get("model") if isinstance(body, dict) else None
        _record_compatibility_rejection(
            http,
            session_id=session_id,
            model=model,
            ingress="anthropic_messages",
            reason=reason,
            path=path,
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "unsupported_schema",
                    "message": "Anthropic Messages request schema is not supported.",
                    "reason": reason,
                    "path": path,
                }
            },
            headers={
                "X-CCIM-Session": session_id,
                "X-CCIM-Compatibility-Reason": reason,
            },
        )

    ctx = RequestContext(session_id=session_id, request=request)
    ctx.extras["feature_flags"] = {
        "compatibility_ingress": "anthropic_messages",
        "compatibility_supported": True,
    }

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
        stream_mode = (
            ctx.extras.get("feature_flags", {}).get("stream_response_mode")
            or "synthesized_complete_sse"
        )

        async def _sse_generator() -> AsyncIterator[bytes]:
            async for chunk in response_dict_to_sse(response_data):
                yield chunk

        return StreamingResponse(
            _sse_generator(),            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-CCIM-Session": session_id,
                "X-CCIM-Stream-Mode": str(stream_mode),
            },
        )

    # -- JSON
    return JSONResponse(
        content=ctx.response_json,
        headers={"X-CCIM-Session": session_id},
    )


@router.post("/chat/completions", response_model=None)
async def create_chat_completion(
    http: Request,
    body: Annotated[Any, Body()],
) -> StreamingResponse | JSONResponse:
    """OpenAI Chat Completions ingress backed by the canonical middleware chain."""
    from ccim.compatibility.openai import (
        CompatibilityError,
        messages_to_openai_response,
        messages_to_openai_sse,
        openai_chat_to_messages,
    )
    from ccim.middleware.chain import RequestContext

    session_id, session_error = _resolve_session_id(http)
    if session_error is not None:
        return session_error
    assert session_id is not None

    try:
        canonical_request = openai_chat_to_messages(body)
    except CompatibilityError as exc:
        _record_compatibility_rejection(
            http,
            session_id=session_id,
            model=body.get("model") if isinstance(body, dict) else None,
            ingress="openai_chat_completions",
            reason=exc.reason,
            path=exc.path,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.as_error(),
            headers={
                "X-CCIM-Session": session_id,
                "X-CCIM-Compatibility-Reason": exc.reason,
            },
        )

    ctx = RequestContext(session_id=session_id, request=canonical_request)
    ctx.extras["feature_flags"] = {
        "compatibility_ingress": "openai_chat_completions",
        "compatibility_supported": True,
    }
    try:
        await http.app.state.chain.run(ctx)
    except httpx.HTTPStatusError as exc:
        logger.error("Chain HTTP error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=exc.response.status_code,
            content=_http_status_error_body(exc),
            headers={"X-CCIM-Session": session_id},
        )
    except Exception as exc:
        logger.error("Chain error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "upstream_error", "message": str(exc)}},
            headers={"X-CCIM-Session": session_id},
        )

    if ctx.blocked:
        error = ctx.response_json if ctx.response_json and "error" in ctx.response_json else {
            "error": {
                "type": "pcfi_block",
                "message": ctx.block_reason or "Request blocked by PCFI.",
            }
        }
        return JSONResponse(
            status_code=ctx.block_status_code,
            content=error,
            headers={"X-CCIM-Session": session_id},
        )
    if ctx.response_json is None:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "type": "upstream_error",
                    "message": "No response from upstream LLM.",
                }
            },
            headers={"X-CCIM-Session": session_id},
        )

    try:
        converted = messages_to_openai_response(
            ctx.response_json,
            requested_model=canonical_request.model,
        )
    except CompatibilityError as exc:
        _record_compatibility_rejection(
            http,
            session_id=session_id,
            model=canonical_request.model,
            ingress="openai_chat_completions",
            reason=exc.reason,
            path=exc.path,
        )
        return JSONResponse(
            status_code=502,
            content=exc.as_error(),
            headers={
                "X-CCIM-Session": session_id,
                "X-CCIM-Compatibility-Reason": exc.reason,
            },
        )

    if canonical_request.stream:
        response_data = ctx.response_json

        async def _openai_sse_generator() -> AsyncIterator[bytes]:
            async for chunk in messages_to_openai_sse(
                response_data,
                requested_model=canonical_request.model,
            ):
                yield chunk

        return StreamingResponse(
            _openai_sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-CCIM-Session": session_id,
                "X-CCIM-Stream-Mode": "synthesized_complete_sse",
            },
        )

    return JSONResponse(
        content=converted,
        headers={"X-CCIM-Session": session_id},
    )


@router.post("/responses", response_model=None)
async def unsupported_responses_api(
    http: Request,
    body: Annotated[Any, Body()],
) -> JSONResponse:
    """Return an explicit contract error for the deferred Responses API."""
    session_id, session_error = _resolve_session_id(http)
    if session_error is not None:
        return session_error
    assert session_id is not None
    reason = "unsupported_responses_api"
    model = body.get("model") if isinstance(body, dict) else None
    _record_compatibility_rejection(
        http,
        session_id=session_id,
        model=model,
        ingress="openai_responses",
        reason=reason,
        path="$",
    )
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": "OpenAI Responses API ingress is not supported.",
                "type": "unsupported_ingress",
                "param": None,
                "code": "CCIM_UNSUPPORTED_RESPONSES_API",
                "ccim_reason": reason,
            }
        },
        headers={
            "X-CCIM-Session": session_id,
            "X-CCIM-Compatibility-Reason": reason,
        },
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


def _resolve_session_id(
    http: Request,
) -> tuple[str | None, JSONResponse | None]:
    """Resolve a marker-safe session from header, launcher token, or prefix."""
    import re

    raw_header = http.headers.get("x-ccim-session")
    if raw_header:
        return _validated_session_id(raw_header)

    launcher_token = ""
    authorization = http.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        launcher_token = authorization[7:].strip()
    if not launcher_token:
        launcher_token = http.headers.get("x-api-key", "").strip()
    token_prefix = "ccim-session-"
    if launcher_token.startswith(token_prefix):
        return _validated_session_id(launcher_token[len(token_prefix) :])

    from ccim.config import get_settings

    prefix = get_settings().session_prefix
    safe_prefix = re.sub(r"[^A-Za-z0-9\-]", "-", prefix) if prefix else ""
    return f"{safe_prefix}{uuid.uuid4()}", None


def _validated_session_id(
    value: str,
) -> tuple[str | None, JSONResponse | None]:
    import re

    if re.fullmatch(r"[A-Za-z0-9\-]+", value):
        return value, None
    return None, JSONResponse(
        status_code=400,
        content={
            "error": {
                "type": "invalid_session_id",
                "message": (
                    "CCIM session id must contain only [A-Za-z0-9-] characters."
                ),
            }
        },
    )


def _record_compatibility_rejection(
    http: Request,
    *,
    session_id: str,
    model: Any,
    ingress: str,
    reason: str,
    path: str,
) -> None:
    """Record a rejected adapter request without forwarding it upstream."""
    from ccim.api.schemas import MessagesRequest
    from ccim.middleware.chain import RequestContext

    safe_model = model if isinstance(model, str) and model else "unsupported"
    ctx = RequestContext(
        session_id=session_id,
        request=MessagesRequest(model=safe_model, messages=[]),
    )
    ctx.pcfi_action = "compatibility_reject"
    ctx.pcfi_reason = reason
    ctx.blocked = True
    ctx.block_status_code = 400
    ctx.block_reason = reason
    ctx.extras["feature_flags"] = {
        "compatibility_ingress": ingress,
        "compatibility_supported": False,
        "compatibility_reason": reason,
        "compatibility_path": path,
    }
    telemetry = getattr(http.app.state, "telemetry_runtime", None)
    if telemetry is not None and hasattr(telemetry, "record"):
        telemetry.record(ctx)


def _anthropic_validation_reason(body: Any) -> tuple[str, str]:
    if not isinstance(body, dict):
        return "request_not_object", "$"
    messages = body.get("messages")
    if not isinstance(messages, list):
        return "invalid_messages", "messages"
    supported_block_types = {"text", "tool_use", "tool_result"}
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            return "message_not_object", f"messages[{message_index}]"
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            path = f"messages[{message_index}].content[{block_index}]"
            if (
                not isinstance(block, dict)
                or block.get("type") not in supported_block_types
            ):
                return "unsupported_content_block", path
    return "invalid_anthropic_schema", "$"
