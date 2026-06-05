"""미들웨어 체인 패턴 + V1 기본 6개 stage 구현.

체인 순서: PCFI → Compress → ForwardAndIntercept → WriteRemap → Telemetry

각 stage는 `RequestContext`를 받아 변형 후 `await call_next(ctx)` 호출.
V2/V3에서 새 미들웨어를 삽입만 하면 기존 코드 수정 불필요 (OCP).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from ccim.api.schemas import (
    ContentBlock,
    Message,
    MessagesRequest,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from ccim.utils.tokens import estimate_text_tokens

_logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# RequestContext
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RequestContext:
    """미들웨어 체인 전체에서 공유되는 가변 상태."""

    session_id: str
    request: MessagesRequest

    # ─ PCFI 결과 ─
    pcfi_action: str | None = None
    pcfi_reason: str | None = None

    # ─ 토큰 측정 ─
    tokens_input_original: int | None = None
    tokens_input_compressed: int | None = None
    tokens_output: int | None = None

    # ─ 루프 카운터 ─
    retrieve_original_calls: int = 0
    write_remaps: int = 0

    # ─ 차단 여부 ─
    blocked: bool = False
    block_status_code: int = 400
    block_reason: str | None = None

    # ─ 응답 (ForwardAndIntercept stage가 채움) ─
    response_json: dict[str, Any] | None = None

    # ─ 단계별 latency (ms) ─
    timings_ms: dict[str, int] = field(default_factory=dict)

    # ─ 자유 확장 슬롯 (V2/V3 미들웨어) ─
    extras: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# 인터페이스
# ─────────────────────────────────────────────────────────────────────

NextCallable = Callable[[RequestContext], Awaitable[None]]


class Middleware(Protocol):
    """모든 미들웨어가 구현해야 할 인터페이스."""

    name: str

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None: ...


# ─────────────────────────────────────────────────────────────────────
# MiddlewareChain
# ─────────────────────────────────────────────────────────────────────


class MiddlewareChain:
    """선언된 순서대로 미들웨어를 실행."""

    def __init__(self, stages: list[Middleware]) -> None:
        self._stages = stages

    async def run(self, ctx: RequestContext) -> None:
        """재귀적 클로저로 체인 실행. 각 stage는 call_next를 호출해 다음 단계로 진행."""

        async def _run_from(idx: int, c: RequestContext) -> None:
            if idx >= len(self._stages):
                return

            async def call_next(c2: RequestContext) -> None:
                await _run_from(idx + 1, c2)

            await self._stages[idx](c, call_next)

        await _run_from(0, ctx)


# ─────────────────────────────────────────────────────────────────────
# Stage 1 — PCFI
# ─────────────────────────────────────────────────────────────────────


class PCFIMiddleware:
    """설계 §3.2.1 — 4-구획 분리 + Llama Guard 검증."""

    name = "pcfi"

    def __init__(self, enforcer: Any) -> None:  # PCFIEnforcer (순환 임포트 방지 Any)
        self._enforcer = enforcer

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        from ccim.pcfi.compartments import Compartments
        from ccim.pcfi.enforcer import PCFIAction

        t0 = time.perf_counter()
        compartments = Compartments.from_request(
            system=ctx.request.system,
            messages=ctx.request.messages,
            tools=ctx.request.tools,
        )
        verdict = await self._enforcer.check(compartments)
        ctx.timings_ms["pcfi"] = int((time.perf_counter() - t0) * 1000)
        ctx.pcfi_action = verdict.action.value
        ctx.pcfi_reason = verdict.reason

        if verdict.action == PCFIAction.BLOCK:
            ctx.blocked = True
            ctx.block_status_code = 400
            ctx.block_reason = verdict.reason
            return  # 체인 중단

        await call_next(ctx)


# ─────────────────────────────────────────────────────────────────────
# Stage 2 — Compress
# ─────────────────────────────────────────────────────────────────────

# 코드 펜스 추출용 (그룹1=prefix, 그룹2=언어태그, 그룹3=코드, 그룹4=closing)
_CODE_FENCE_RE = re.compile(
    r"(```(python|py|java|csharp|c#|cs)?\s*\n)(.*?)(```)",
    re.DOTALL | re.IGNORECASE,
)

# LLM 응답 스캔용 마커 패턴 (markers.py의 _MARKER_RE와 동일)
_ORPHAN_MARKER_RE = re.compile(
    r"<<CTX_(?P<session>[A-Za-z0-9\-]+):(?P<ctx>[A-Za-z0-9\-_]+)>>"
)


def _csv_names(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def _tool_result_content_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return ""


def _normalize_tool_path(path: str) -> str:
    return path.strip().replace("\\", "/").lower()


def _tool_input_path(tool_input: dict[str, Any]) -> str | None:
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _normalize_tool_path(raw)


class CompressMiddleware:
    """설계 §3.2.2 — 트리거 휴리스틱 + AST 압축 + Redis 저장."""

    name = "compress"

    def __init__(
        self, compressor: Any, store: Any, settings: Any, *, compress_enabled: bool = True
    ) -> None:
        self._compressor = compressor  # ASTCompressor
        self._store = store            # ReversibilityStore
        self._settings = settings      # Settings
        self._compress_enabled = compress_enabled

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        from ccim.compress.trigger import (
            has_compressible_content,
            select_compression_candidates,
        )

        if not self._compress_enabled:
            from ccim.utils.tokens import estimate_request_tokens

            total = estimate_request_tokens(ctx.request)
            ctx.tokens_input_original = ctx.tokens_input_compressed = total
            ctx.extras["feature_flags"] = {
                "compress_enabled": False,
                "compress_skip_reason": "disabled",
            }
            await call_next(ctx)
            return
        from ccim.reversibility.retrieve_tool import (
            RETRIEVE_ORIGINAL_TOOL,
            build_system_hint,
        )
        from ccim.utils.tokens import estimate_request_tokens

        t0 = time.perf_counter()

        # 원본 토큰 측정
        ctx.tokens_input_original = estimate_request_tokens(ctx.request)
        _logger.info(
            "[compress] session=%s total_tokens=%d threshold=%d",
            ctx.session_id, ctx.tokens_input_original,
            self._settings.compression_trigger_tokens,
        )

        compress_stats: dict[str, Any] = {
            "compress_enabled": True,
            "current_turn_compression_enabled": self._settings.current_turn_compression_enabled,
            "compress_current_turn_threshold_tokens": (
                self._settings.current_turn_compression_trigger_tokens
            ),
            "compression_cluster_summary_enabled": getattr(
                self._settings, "compression_cluster_summary_enabled", False
            ),
            "compress_candidates": 0,
            "compress_candidate_messages": 0,
            "compress_ast_blocks": 0,
            "compress_structured_summaries": 0,
            "compress_tool_result_refs": 0,
            "compress_tool_result_stores": 0,
            "compress_history_contexts": 0,
            "compress_history_candidate_messages": 0,
            "compress_current_turn_candidates": 0,
            "compress_current_turn_contexts": 0,
            "compress_current_turn_allowed_tools": self._settings.current_turn_compression_read_tools,
            "compress_current_turn_tool_results": 0,
            "compress_current_turn_allowed_tool_results": 0,
            "compress_current_turn_rejected_tool_results": 0,
            "compress_current_turn_compressible_tool_results": 0,
            "compress_current_turn_raw_chars_max": 0,
            "compress_current_turn_raw_lines_max": 0,
            "compress_current_turn_matched_tool_names": [],
            "compress_current_turn_rejected_tool_names": [],
            "compress_current_turn_source_paths": [],
            "compress_current_turn_source_path_results": 0,
            "compress_current_turn_missing_source_paths": 0,
            "compress_current_turn_missing_source_path_tool_results": [],
            "compress_tool_result_attempts": 0,
            "compress_tool_result_ast_successes": 0,
            "compress_tool_result_failures": 0,
            "compress_tool_result_last_fail_reason": None,
            "compress_tool_result_detected_languages": [],
            "compress_tool_result_raw_chars_max": 0,
            "compress_tool_result_raw_lines_max": 0,
            "compress_tool_result_store_context_successes": 0,
            "compress_tool_result_store_context_failures": 0,
            "compress_tool_result_store_blob_failures": 0,
            "compress_text_attempts": 0,
            "compress_text_ast_successes": 0,
            "compress_text_failures": 0,
            "compress_text_last_fail_reason": None,
            "compress_text_fence_count": 0,
            "compress_text_raw_chars_max": 0,
            "compress_text_raw_lines_max": 0,
            "compress_context_metadata_count": 0,
            "compress_context_symbol_names": [],
            "compress_context_original_ranges": [],
            "compress_skip_reason": None,
        }

        candidates, diagnostics = select_compression_candidates(
            ctx.request.messages,
            threshold_tokens=self._settings.compression_trigger_tokens,
            target_tokens=self._settings.compression_target_tokens,
            request_tokens=ctx.tokens_input_original,
        )
        compress_stats.update(diagnostics.as_feature_flags())
        current_turn_allowed = self._current_turn_allowed_tool_results(
            ctx.request.messages,
            total_tokens=ctx.tokens_input_original,
        )
        self._record_current_turn_diagnostics(compress_stats, ctx.request.messages)
        current_turn_candidate_ids = {id(m) for m in current_turn_allowed}
        if current_turn_allowed:
            compress_stats["compress_current_turn_candidates"] = len(current_turn_allowed)
            existing_ids = {id(m) for m in candidates}
            candidates.extend(m for m in current_turn_allowed if id(m) not in existing_ids)
            if candidates:
                compress_stats["compress_skip_reason"] = None
        elif (
            self._settings.current_turn_compression_enabled
            and ctx.tokens_input_original
            < self._settings.current_turn_compression_trigger_tokens
            and compress_stats["compress_current_turn_allowed_tool_results"] > 0
            and compress_stats["compress_current_turn_compressible_tool_results"] > 0
        ):
            compress_stats["compress_skip_reason"] = "current_turn_below_threshold"

        if not candidates:
            _logger.info(
                "[compress] skip — %s (eligible=%d compressible=%d current_turn_excluded=%d)",
                diagnostics.skip_reason,
                diagnostics.eligible_messages,
                diagnostics.compressible_messages,
                diagnostics.current_turn_excluded,
            )
            ctx.extras["feature_flags"] = {
                **ctx.extras.get("feature_flags", {}),
                **compress_stats,
            }
            ctx.tokens_input_compressed = ctx.tokens_input_original
            ctx.timings_ms["compress"] = int((time.perf_counter() - t0) * 1000)
            await call_next(ctx)
            return

        _logger.info("[compress] %d candidate message(s) selected for compression", len(candidates))
        compress_stats["compress_candidates"] = len(candidates)
        # 압축 수행
        candidate_set = {id(m) for m in candidates}
        new_messages: list[Message] = []
        any_compressed = False
        all_context_ids: list[str] = []

        for msg in ctx.request.messages:
            if id(msg) not in candidate_set or not has_compressible_content(msg):
                new_messages.append(msg)
                continue

            allowed_tool_result_ids = None
            source_paths_by_tool_result_id = None
            context_sources_by_id: dict[str, str] = {}
            current_turn_msg = id(msg) in current_turn_candidate_ids
            if current_turn_msg:
                allowed_tool_result_ids = self._allowed_current_turn_tool_result_ids(
                    ctx.request.messages, msg
                )
                source_paths_by_tool_result_id = (
                    self._allowed_current_turn_source_paths_by_tool_result_id(
                        ctx.request.messages, msg, allowed_tool_result_ids
                    )
                )

            compressed_msg, did_compress, ctx_ids = await self._compress_message(
                msg,
                ctx.session_id,
                compress_stats,
                allowed_tool_result_ids=allowed_tool_result_ids,
                source_paths_by_tool_result_id=source_paths_by_tool_result_id,
                context_sources_by_id=context_sources_by_id,
            )
            new_messages.append(compressed_msg)
            if did_compress:
                any_compressed = True
                compress_stats["compress_candidate_messages"] += 1
                all_context_ids.extend(ctx_ids)
                if current_turn_msg:
                    ctx.extras.setdefault("current_turn_context_ids", []).extend(ctx_ids)
                    source_paths = set(context_sources_by_id.values()) or set(
                        (source_paths_by_tool_result_id or {}).values()
                    )
                    self._record_current_turn_sources(
                        ctx,
                        ctx_ids,
                        source_paths,
                        context_sources_by_id=context_sources_by_id,
                    )
                    if source_paths:
                        known_paths = set(
                            compress_stats.get("compress_current_turn_source_paths") or []
                        )
                        known_paths.update(source_paths)
                        compress_stats["compress_current_turn_source_paths"] = sorted(
                            known_paths
                        )
                    compress_stats["compress_current_turn_contexts"] += len(ctx_ids)
                else:
                    compress_stats["compress_history_candidate_messages"] += 1
                    compress_stats["compress_history_contexts"] += len(ctx_ids)

        # retrieve_original 도구 주입 (압축이 실제로 일어난 경우 + 플래그 활성화 시만)
        # compression_enable_retrieve=False (기본값): 토큰 절감만, LLM 추가 라운드트립 없음
        # compression_enable_retrieve=True         : 완전한 가역성, LLM이 원본 복원 가능
        if all_context_ids and self._settings.compression_enable_retrieve:
            existing_tools = list(ctx.request.tools or [])
            tool_names = {t.name for t in existing_tools}
            if "retrieve_original" not in tool_names:
                existing_tools.insert(
                    0,
                    ToolDefinition(
                        name=RETRIEVE_ORIGINAL_TOOL["name"],
                        description=RETRIEVE_ORIGINAL_TOOL["description"],
                        input_schema=RETRIEVE_ORIGINAL_TOOL["input_schema"],
                    ),
                )

            # 시스템 힌트 주입
            hint = build_system_hint()
            existing_system = ctx.request.system
            if existing_system is None:
                new_system: str | list[ContentBlock] = hint
            elif isinstance(existing_system, str):
                new_system = f"{existing_system}\n\n{hint}"
            else:
                new_system = [*list(existing_system), TextBlock(text=hint)]

            ctx.request = ctx.request.model_copy(
                update={
                    "messages": new_messages,
                    "tools": existing_tools,
                    "system": new_system,
                }
            )
        else:
            # 압축 미발생 또는 retrieve 비활성화 — 메시지만 교체
            ctx.request = ctx.request.model_copy(update={"messages": new_messages})

        ctx.tokens_input_compressed = estimate_request_tokens(ctx.request)
        saved = ctx.tokens_input_original - ctx.tokens_input_compressed
        _logger.info(
            "[compress] done — original=%d compressed=%d saved=%d (%.1f%%) any_compressed=%s",
            ctx.tokens_input_original, ctx.tokens_input_compressed, saved,
            saved / ctx.tokens_input_original * 100 if ctx.tokens_input_original else 0,
            any_compressed,
        )
        # WriteRemap이 라인 역변환에 사용할 context_id 목록 저장
        # all_context_ids: 이번 요청에서 생성된 모든 압축 컨텍스트 ID
        # active_context_id: 단일 컨텍스트일 때만 설정 (다중 시 WriteRemap fallback 비활성화)
        ctx.extras["all_context_ids"] = all_context_ids
        if len(all_context_ids) == 1:
            ctx.extras["active_context_id"] = all_context_ids[0]
        ctx.extras["feature_flags"] = {
            **ctx.extras.get("feature_flags", {}),
            **compress_stats,
            "compress_any": any_compressed,
            "compress_context_ids": len(all_context_ids),
            "compress_saved_tokens_est": saved,
        }
        ctx.timings_ms["compress"] = int((time.perf_counter() - t0) * 1000)
        await call_next(ctx)

    async def _compress_message(
        self,
        msg: Message,
        session_id: str,
        stats: dict[str, Any] | None = None,
        *,
        allowed_tool_result_ids: set[str] | None = None,
        source_paths_by_tool_result_id: dict[str, str] | None = None,
        context_sources_by_id: dict[str, str] | None = None,
    ) -> tuple[Message, bool, list[str]]:
        """메시지 내 Python 코드를 압축.

        두 가지 경로:
        - TextBlock   : ```python``` 코드 펜스 내 Python 압축 (기존)
        - ToolResultBlock : 파일 읽기로 들어온 raw Python 직접 압축 (신규)

        반환: (compressed_message, did_compress, all_context_ids)
        """
        if isinstance(msg.content, str):
            new_text, did, ctx_ids = await self._compress_text(
                msg.content, session_id, stats
            )
            if did:
                if stats is not None:
                    stats["compress_ast_blocks"] += len(ctx_ids)
                return Message(role=msg.role, content=new_text), True, ctx_ids
            return msg, False, []

        new_blocks: list[ContentBlock] = []
        any_did = False
        all_ctx_ids: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                new_text, did, ctx_ids = await self._compress_text(
                    block.text, session_id, stats
                )
                new_blocks.append(TextBlock(text=new_text))
                if did:
                    any_did = True
                    if stats is not None:
                        stats["compress_ast_blocks"] += len(ctx_ids)
                    all_ctx_ids.extend(ctx_ids)
            elif isinstance(block, ToolResultBlock):
                if (
                    allowed_tool_result_ids is not None
                    and block.tool_use_id not in allowed_tool_result_ids
                ):
                    new_blocks.append(block)
                    continue
                new_block, did, ctx_ids = await self._compress_tool_result(
                    block,
                    session_id,
                    stats,
                    source_path=(
                        source_paths_by_tool_result_id or {}
                    ).get(block.tool_use_id),
                )
                new_blocks.append(new_block)
                if did:
                    any_did = True
                    all_ctx_ids.extend(ctx_ids)
                    source_path = (source_paths_by_tool_result_id or {}).get(
                        block.tool_use_id
                    )
                    if context_sources_by_id is not None and source_path:
                        for context_id in ctx_ids:
                            context_sources_by_id[context_id] = source_path
            else:
                new_blocks.append(block)

        if any_did:
            return Message(role=msg.role, content=new_blocks), True, all_ctx_ids
        return msg, False, []

    def _current_turn_allowed_tool_results(
        self,
        messages: list[Message],
        *,
        total_tokens: int,
    ) -> list[Message]:
        """Return current-turn user messages containing allowed Read/Search tool results."""
        if not self._settings.current_turn_compression_enabled:
            return []
        if total_tokens < self._settings.current_turn_compression_trigger_tokens:
            return []
        last_user_idx = -1
        for i, msg in enumerate(messages):
            if msg.role == "user":
                last_user_idx = i
        if last_user_idx < 0:
            return []

        allowed_messages: list[Message] = []
        for msg in messages[last_user_idx:]:
            if msg.role != "user" or not isinstance(msg.content, list):
                continue
            if self._allowed_current_turn_tool_result_ids(messages, msg):
                allowed_messages.append(msg)
        return allowed_messages

    def _record_current_turn_diagnostics(
        self, stats: dict[str, Any], messages: list[Message]
    ) -> None:
        """Record why current-turn ToolResults are or are not eligible."""
        from ccim.compress.trigger import get_tool_result_text, has_compressible_content

        if not self._settings.current_turn_compression_enabled:
            return
        last_user_idx = -1
        for i, msg in enumerate(messages):
            if msg.role == "user":
                last_user_idx = i
        if last_user_idx < 0:
            return

        allowed_tools = _csv_names(self._settings.current_turn_compression_read_tools)
        matched_names: set[str] = set()
        rejected_names: set[str] = set()
        missing_source_path_tool_results: set[str] = set()
        total = 0
        allowed = 0
        rejected = 0
        compressible = 0
        source_path_results = 0
        max_chars = 0
        max_lines = 0

        for msg in messages[last_user_idx:]:
            if msg.role != "user" or not isinstance(msg.content, list):
                continue
            for block in msg.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                total += 1
                tool_name = self._tool_use_name_for(messages, block.tool_use_id)
                normalized_name = tool_name.lower()
                if normalized_name in allowed_tools:
                    allowed += 1
                    if tool_name:
                        matched_names.add(tool_name)
                    if _tool_input_path(
                        self._tool_use_input_for(messages, block.tool_use_id)
                    ):
                        source_path_results += 1
                    else:
                        missing_source_path_tool_results.add(block.tool_use_id)
                else:
                    rejected += 1
                    rejected_names.add(tool_name or "<missing_tool_use>")

                raw = get_tool_result_text(block)
                max_chars = max(max_chars, len(raw))
                max_lines = max(max_lines, raw.count("\n") + 1 if raw else 0)
                if has_compressible_content(Message(role=msg.role, content=[block])):
                    compressible += 1

        stats["compress_current_turn_tool_results"] = total
        stats["compress_current_turn_allowed_tool_results"] = allowed
        stats["compress_current_turn_rejected_tool_results"] = rejected
        stats["compress_current_turn_compressible_tool_results"] = compressible
        stats["compress_current_turn_raw_chars_max"] = max_chars
        stats["compress_current_turn_raw_lines_max"] = max_lines
        stats["compress_current_turn_matched_tool_names"] = sorted(matched_names)
        stats["compress_current_turn_rejected_tool_names"] = sorted(rejected_names)
        stats["compress_current_turn_source_path_results"] = source_path_results
        stats["compress_current_turn_missing_source_paths"] = len(
            missing_source_path_tool_results
        )
        stats["compress_current_turn_missing_source_path_tool_results"] = sorted(
            missing_source_path_tool_results
        )

    def _allowed_current_turn_tool_result_ids(
        self, messages: list[Message], msg: Message
    ) -> set[str]:
        if not isinstance(msg.content, list):
            return set()
        allowed_tools = _csv_names(self._settings.current_turn_compression_read_tools)
        if not allowed_tools:
            return set()
        tool_ids = {
            block.tool_use_id
            for block in msg.content
            if isinstance(block, ToolResultBlock)
            and self._tool_use_name_for(messages, block.tool_use_id).lower() in allowed_tools
        }
        return tool_ids

    def _allowed_current_turn_source_paths(
        self,
        messages: list[Message],
        msg: Message,
        allowed_tool_result_ids: set[str],
    ) -> set[str]:
        return set(
            self._allowed_current_turn_source_paths_by_tool_result_id(
                messages, msg, allowed_tool_result_ids
            ).values()
        )

    def _allowed_current_turn_source_paths_by_tool_result_id(
        self,
        messages: list[Message],
        msg: Message,
        allowed_tool_result_ids: set[str],
    ) -> dict[str, str]:
        if not isinstance(msg.content, list):
            return {}
        paths: dict[str, str] = {}
        for block in msg.content:
            if (
                isinstance(block, ToolResultBlock)
                and block.tool_use_id in allowed_tool_result_ids
            ):
                tool_input = self._tool_use_input_for(messages, block.tool_use_id)
                path = _tool_input_path(tool_input)
                if path:
                    paths[block.tool_use_id] = path
        return paths

    @staticmethod
    def _record_current_turn_sources(
        ctx: RequestContext,
        context_ids: list[str],
        source_paths: set[str],
        *,
        context_sources_by_id: dict[str, str] | None = None,
    ) -> None:
        mapped_context_ids: set[str] = set()
        path_set = ctx.extras.setdefault("current_turn_source_paths", set())
        if source_paths and isinstance(path_set, set):
            path_set.update(source_paths)
        sources = ctx.extras.setdefault("current_turn_context_sources", {})
        if isinstance(sources, dict) and context_sources_by_id:
            sources.update(context_sources_by_id)
            mapped_context_ids.update(context_sources_by_id)
        elif isinstance(sources, dict) and len(source_paths) == 1:
            source_path = next(iter(source_paths))
            for context_id in context_ids:
                sources[context_id] = source_path
                mapped_context_ids.add(context_id)

        missing_context_ids = sorted(
            context_id for context_id in context_ids if context_id not in mapped_context_ids
        )
        if missing_context_ids:
            missing = ctx.extras.setdefault(
                "current_turn_context_source_missing_ids", []
            )
            if isinstance(missing, list):
                for context_id in missing_context_ids:
                    if context_id not in missing:
                        missing.append(context_id)

    @staticmethod
    def _tool_use_name_for(messages: list[Message], tool_use_id: str) -> str:
        for msg in reversed(messages):
            if not isinstance(msg.content, list):
                continue
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.id == tool_use_id:
                    return block.name
        return ""

    @staticmethod
    def _tool_use_input_for(messages: list[Message], tool_use_id: str) -> dict[str, Any]:
        for msg in reversed(messages):
            if not isinstance(msg.content, list):
                continue
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.id == tool_use_id:
                    return block.input
        return {}

    async def _compress_tool_result(
        self,
        block: ToolResultBlock,
        session_id: str,
        stats: dict[str, Any] | None = None,
        *,
        source_path: str | None = None,
    ) -> tuple[ToolResultBlock, bool, list[str]]:
        """ToolResultBlock 내 raw 코드(Python/Java/C#)를 ASTCompressor로 직접 압축.

        파일 읽기(Read 도구) 결과는 코드 펜스 없이 raw 코드가 들어오므로
        언어를 자동 감지한 뒤 compress() 호출.
        반환: (new_block, did_compress, context_ids)
        """
        from ccim.compress.structured_outputs import (
            build_tool_result_reference,
            should_dedupe_tool_result,
            summarize_command_output,
            tool_result_hash,
        )
        from ccim.compress.trigger import (
            detect_language_from_code,
            get_tool_result_text,
            is_raw_code_tool_result,
        )
        from ccim.reversibility.store import ToolResultRecord

        raw = get_tool_result_text(block)
        if not raw:
            self._record_tool_result_failure(stats, "empty_raw")
            return block, False, []

        self._record_tool_result_attempt(stats, raw)
        if not is_raw_code_tool_result(block):
            content_hash = tool_result_hash(raw)
            line_count = raw.count("\n") + 1
            can_dedupe = should_dedupe_tool_result(raw)

            if can_dedupe and hasattr(self._store, "get_tool_result"):
                try:
                    existing = await self._store.get_tool_result(session_id, content_hash)
                except Exception:
                    existing = None
                if existing is not None:
                    if stats is not None:
                        stats["compress_tool_result_refs"] += 1
                    reference = build_tool_result_reference(
                        content_hash,
                        chars=len(raw),
                        lines=line_count,
                    )
                    return (
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=reference,
                            is_error=block.is_error,
                        ),
                        True,
                        [],
                    )

            if can_dedupe and hasattr(self._store, "put_tool_result"):
                try:
                    await self._store.put_tool_result(
                        ToolResultRecord(
                            session_id=session_id,
                            content_hash=content_hash,
                            content=raw,
                            metadata={"chars": len(raw), "lines": line_count},
                        )
                    )
                    if stats is not None:
                        stats["compress_tool_result_stores"] += 1
                except Exception:
                    if stats is not None:
                        stats["compress_tool_result_store_blob_failures"] += 1

            summarized = summarize_command_output(raw, is_error=block.is_error)
            if summarized is not None:
                if stats is not None:
                    stats["compress_structured_summaries"] += 1
                return (
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=summarized,
                        is_error=block.is_error,
                    ),
                    True,
                    [],
                )
            self._record_tool_result_failure(stats, "not_code_or_structured")
            return block, False, []

        language = detect_language_from_code(raw)
        self._record_tool_result_language(stats, language)
        ctx_prefix = uuid.uuid4().hex[:8]  # 호출 단위 nonce
        try:
            comp_result = self._compressor.compress(
                raw,
                session_id=session_id,
                language=language,
                ctx_prefix=ctx_prefix,
                cluster_repeated_functions=getattr(
                    self._settings, "compression_cluster_summary_enabled", False
                ),
            )
        except Exception:
            self._record_tool_result_failure(stats, "compress_exception")
            return block, False, []

        if not comp_result.blocks:
            self._record_tool_result_failure(stats, "no_ast_blocks")
            return block, False, []

        # store 실패 시 원본 블록 반환 (고아 마커 방지)
        try:
            await self._store_blocks(
                comp_result, session_id, language=language, source_path=source_path
            )
        except Exception:
            self._record_tool_result_failure(stats, "store_context_failed")
            if stats is not None:
                stats["compress_tool_result_store_context_failures"] += 1
            return block, False, []

        ctx_ids = [f"{session_id}:{b.context_id}" for b in comp_result.blocks]
        if stats is not None:
            stats["compress_ast_blocks"] += len(ctx_ids)
            stats["compress_tool_result_ast_successes"] += 1
            stats["compress_tool_result_store_context_successes"] += 1
            self._record_context_metadata(stats, comp_result.blocks, source_path)

        # 압축된 텍스트로 ToolResultBlock 교체
        new_block = ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=comp_result.compressed_text,
            is_error=block.is_error,
        )
        return new_block, True, ctx_ids

    @staticmethod
    def _record_tool_result_attempt(stats: dict[str, Any] | None, raw: str) -> None:
        if stats is None:
            return
        stats["compress_tool_result_attempts"] += 1
        stats["compress_tool_result_raw_chars_max"] = max(
            stats["compress_tool_result_raw_chars_max"], len(raw)
        )
        stats["compress_tool_result_raw_lines_max"] = max(
            stats["compress_tool_result_raw_lines_max"], raw.count("\n") + 1
        )

    @staticmethod
    def _record_tool_result_language(stats: dict[str, Any] | None, language: str) -> None:
        if stats is None:
            return
        languages = set(stats.get("compress_tool_result_detected_languages") or [])
        languages.add(language)
        stats["compress_tool_result_detected_languages"] = sorted(languages)

    @staticmethod
    def _record_tool_result_failure(stats: dict[str, Any] | None, reason: str) -> None:
        if stats is None:
            return
        stats["compress_tool_result_failures"] += 1
        stats["compress_tool_result_last_fail_reason"] = reason

    @staticmethod
    def _record_context_metadata(
        stats: dict[str, Any], blocks: list[Any], source_path: str | None
    ) -> None:
        stats["compress_context_metadata_count"] += len(blocks)
        symbols = set(stats.get("compress_context_symbol_names") or [])
        ranges = set(stats.get("compress_context_original_ranges") or [])
        for block in blocks:
            symbol_name = getattr(block, "symbol_name", None)
            if isinstance(symbol_name, str) and symbol_name:
                symbols.add(symbol_name)
            original_lines = getattr(block, "original_lines", None)
            if (
                isinstance(source_path, str)
                and isinstance(original_lines, tuple)
                and len(original_lines) == 2
            ):
                ranges.add(f"{source_path}:{original_lines[0]}-{original_lines[1]}")
        stats["compress_context_symbol_names"] = sorted(symbols)[:20]
        stats["compress_context_original_ranges"] = sorted(ranges)[:20]

    @staticmethod
    def _record_text_attempt(
        stats: dict[str, Any] | None, text: str, *, fence_count: int
    ) -> None:
        if stats is None:
            return
        stats["compress_text_attempts"] += 1
        stats["compress_text_fence_count"] += fence_count
        stats["compress_text_raw_chars_max"] = max(
            stats["compress_text_raw_chars_max"], len(text)
        )
        stats["compress_text_raw_lines_max"] = max(
            stats["compress_text_raw_lines_max"], text.count("\n") + 1
        )

    @staticmethod
    def _record_text_failure(stats: dict[str, Any] | None, reason: str) -> None:
        if stats is None:
            return
        stats["compress_text_failures"] += 1
        stats["compress_text_last_fail_reason"] = reason

    async def _compress_text(
        self, text: str, session_id: str, stats: dict[str, Any] | None = None
    ) -> tuple[str, bool, list[str]]:
        """텍스트 내 모든 코드 펜스(Python/Java/C#) 압축.

        store 성공이 확인된 펜스만 치환한다.
        store 실패 시 해당 펜스는 원본을 그대로 유지 (고아 마커 방지).
        반환: (new_text, did_compress, active_context_ids)
        """
        from ccim.compress.trigger import detect_language_from_fence

        matches = list(_CODE_FENCE_RE.finditer(text))
        self._record_text_attempt(stats, text, fence_count=len(matches))
        if not matches:
            self._record_text_failure(stats, "no_code_fence")
            return text, False, []

        # (start, end, replacement) — store 성공한 펜스만 기록
        replacements: list[tuple[int, int, str]] = []
        active_ctx_ids: list[str] = []

        for m in matches:
            prefix = m.group(1)        # ```java\n
            fence_tag = m.group(2)     # java / python / None
            code = m.group(3)          # code body
            suffix = m.group(4)        # ```
            language = detect_language_from_fence(fence_tag)
            ctx_prefix = uuid.uuid4().hex[:8]  # 호출 단위 nonce — session 내 키 충돌 방지

            try:
                comp_result = self._compressor.compress(
                    code,
                    session_id=session_id,
                    language=language,
                    ctx_prefix=ctx_prefix,
                    cluster_repeated_functions=getattr(
                        self._settings, "compression_cluster_summary_enabled", False
                    ),
                )
            except Exception:
                self._record_text_failure(stats, "compress_exception")
                continue  # 파싱 실패 → 원본 유지

            if not comp_result.blocks:
                self._record_text_failure(stats, "no_ast_blocks")
                continue  # 압축 대상 없음 → 원본 유지

            # store 먼저 — 실패 시 이 펜스 건너뜀 (마커 삽입 안 함)
            try:
                await self._store_blocks(comp_result, session_id, language=language)
            except Exception:
                self._record_text_failure(stats, "store_context_failed")
                continue

            replacements.append(
                (m.start(), m.end(), f"{prefix}{comp_result.compressed_text}{suffix}")
            )
            active_ctx_ids.append(f"{session_id}:{comp_result.blocks[0].context_id}")

        if not replacements:
            self._record_text_failure(stats, "no_replacements")
            return text, False, []
        if stats is not None:
            stats["compress_text_ast_successes"] += len(replacements)

        # 역순 치환으로 인덱스 보존
        result = text
        for start, end, repl in reversed(replacements):
            result = result[:start] + repl + result[end:]

        return result, True, active_ctx_ids

    async def _store_blocks(
        self,
        comp_result: Any,
        session_id: str,
        *,
        language: str = "python",
        source_path: str | None = None,
    ) -> None:
        from ccim.reversibility.store import ContextRecord

        for block in comp_result.blocks:
            record = ContextRecord(
                session_id=session_id,
                context_id=block.context_id,
                original_code=block.original_code,
                language=language,
                line_mapping=comp_result.line_mapping,
                source_path=source_path,
                symbol_name=getattr(block, "symbol_name", None),
                original_lines=block.original_lines,
            )
            await self._store.put(record)


# ─────────────────────────────────────────────────────────────────────
# Stage 3 — ForwardAndIntercept
# ─────────────────────────────────────────────────────────────────────

_RETRIEVE_TOOL_NAME = "retrieve_original"


class ForwardAndInterceptMiddleware:
    """설계 §4.1 + §4.2 — LLM 호출 + retrieve_original 인터셉트 루프."""

    name = "forward"

    def __init__(self, llm_client: Any, interceptor: Any, max_loops: int = 5, model_override: str | None = None) -> None:
        self._client = llm_client    # LLMClient
        self._interceptor = interceptor  # ReversibilityInterceptor
        self._max_loops = max_loops
        self._model_override = model_override  # CCIM_LLM_MODEL 설정값으로 모델명 강제 치환

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        t0 = time.perf_counter()
        flags = ctx.extras.setdefault("feature_flags", {})
        requested_stream = bool(ctx.request.stream)
        flags.update(
            {
                "stream_requested": requested_stream,
                "stream_response_mode": (
                    "synthesized_complete_sse" if requested_stream else "json"
                ),
                "stream_realtime_relay_enabled": False,
                "stream_policy_reason": (
                    "retrieve_loop_requires_complete_intercept"
                    if requested_stream
                    else None
                ),
                "retrieve_loop_limit": self._max_loops,
                "retrieve_loop_iterations": 0,
                "retrieve_original_tool_uses": 0,
                "retrieve_original_store_fetches": 0,
                "retrieve_original_cache_hits": 0,
                "retrieve_original_hits": 0,
                "retrieve_original_misses": 0,
                "retrieve_original_bulk_tool_uses": 0,
                "retrieve_original_context_ids": 0,
                "retrieve_original_tool_use_tokens_est": 0,
                "retrieve_original_result_tokens_est": 0,
                "retrieve_original_result_chars": 0,
                "retrieve_original_loop_limit_exceeded": False,
            }
        )
        retrieve_cache: dict[str, tuple[str, bool]] = {}

        # CCIM_LLM_MODEL이 설정된 경우 upstream 모델명으로 치환 (Roo Code 등이 Claude 모델명 전달 시)
        request_updates: dict[str, Any] = {"stream": False}
        if self._model_override:
            request_updates["model"] = self._model_override
        working_request = ctx.request.model_copy(update=request_updates)
        response_dict: dict[str, Any] = {}

        for loop_idx in range(self._max_loops):
            flags["retrieve_loop_iterations"] = loop_idx + 1
            response_dict = await self._client.complete(working_request)

            # retrieve_original tool_use 블록 탐색
            content = response_dict.get("content") or []
            retrieve_blocks = [
                b for b in content
                if isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("name") == _RETRIEVE_TOOL_NAME
            ]

            if not retrieve_blocks:
                break  # 일반 응답 — 루프 종료

            # 어시스턴트 메시지 구성 (tool_use 포함)
            assistant_blocks: list[ContentBlock] = []
            for b in content:
                if isinstance(b, dict):
                    btype = b.get("type")
                    if btype == "text":
                        assistant_blocks.append(TextBlock(text=b.get("text", "")))
                    elif btype == "tool_use":
                        assistant_blocks.append(
                            ToolUseBlock(
                                id=b.get("id", f"tu_{uuid.uuid4().hex[:8]}"),
                                name=b.get("name", ""),
                                input=b.get("input", {}),
                            )
                        )

            # tool_result 메시지 구성
            tool_result_blocks: list[ContentBlock] = []
            for b in retrieve_blocks:
                tool_input = b.get("input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}
                ctx.retrieve_original_calls += 1
                flags["retrieve_original_tool_uses"] += 1
                flags["retrieve_original_tool_use_tokens_est"] += estimate_text_tokens(
                    json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                )
                context_ids = self._retrieve_context_ids_from_input(tool_input)
                if context_ids is None:
                    resolution = await self._interceptor.handle_tool_use(
                        tool_input,
                        expected_session_id=ctx.session_id,
                    )
                    content_text = resolution.content
                    is_error = resolution.is_error
                    flags["retrieve_original_store_fetches"] += 1
                    if is_error:
                        flags["retrieve_original_misses"] += 1
                    else:
                        flags["retrieve_original_hits"] += 1
                    context_ids = []
                else:
                    if len(context_ids) > 1 or "context_ids" in tool_input:
                        flags["retrieve_original_bulk_tool_uses"] += 1
                    flags["retrieve_original_context_ids"] += len(context_ids)
                    resolved: list[tuple[str, str, bool]] = []
                    for context_id in context_ids:
                        if context_id in retrieve_cache:
                            item_content, item_error = retrieve_cache[context_id]
                            flags["retrieve_original_cache_hits"] += 1
                        else:
                            resolution = await self._interceptor.handle_tool_use(
                                {"context_id": context_id},
                                expected_session_id=ctx.session_id,
                            )
                            item_content = resolution.content
                            item_error = resolution.is_error
                            retrieve_cache[context_id] = (item_content, item_error)
                            flags["retrieve_original_store_fetches"] += 1
                            if item_error:
                                flags["retrieve_original_misses"] += 1
                            else:
                                flags["retrieve_original_hits"] += 1
                        resolved.append((context_id, item_content, item_error))
                    is_error = any(item_error for _, _, item_error in resolved)
                    content_text = self._format_retrieve_result(resolved)
                flags["retrieve_original_result_tokens_est"] += estimate_text_tokens(
                    content_text
                )
                flags["retrieve_original_result_chars"] += len(content_text)
                for context_id in context_ids:
                    cached = retrieve_cache.get(context_id)
                    if cached is None or cached[1]:
                        continue
                    ctx.extras.setdefault("retrieved_contexts", {})[
                        context_id
                    ] = cached[0]
                tool_result_blocks.append(
                    ToolResultBlock(
                        tool_use_id=b.get("id", ""),
                        content=content_text,
                        is_error=is_error,
                    )
                )

            new_messages = [
                *list(working_request.messages),
                Message(role="assistant", content=assistant_blocks),
                Message(role="user", content=tool_result_blocks),
            ]
            working_request = working_request.model_copy(
                update={"messages": new_messages, "stream": False}
            )

        # 루프 소진 확인 — 아직 미해결 retrieve_original tool_use가 남아있으면 오류
        final_content = response_dict.get("content") or []
        remaining_retrieve = [
            b for b in final_content
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name") == _RETRIEVE_TOOL_NAME
        ]
        if remaining_retrieve:
            flags["retrieve_original_loop_limit_exceeded"] = True
            flags["retrieve_original_unresolved_tool_uses"] = len(remaining_retrieve)
            _logger.error(
                "[forward] retrieve_original loop limit(%d) 소진 — unresolved tool_use %d개",
                self._max_loops,
                len(remaining_retrieve),
            )
            ctx.response_json = {
                "error": {
                    "type": "loop_limit",
                    "message": (
                        f"retrieve_original loop limit ({self._max_loops}) exceeded. "
                        "Upstream LLM did not produce a final response."
                    ),
                }
            }
            ctx.blocked = True
            ctx.block_status_code = 502
            ctx.timings_ms["forward"] = int((time.perf_counter() - t0) * 1000)
            return

        ctx.response_json = response_dict
        usage = response_dict.get("usage") or {}
        ctx.tokens_output = usage.get("output_tokens")
        ctx.timings_ms["forward"] = int((time.perf_counter() - t0) * 1000)
        await call_next(ctx)

    @staticmethod
    def _retrieve_context_ids_from_input(tool_input: dict[str, Any]) -> list[str] | None:
        if "context_ids" in tool_input:
            raw_many = tool_input.get("context_ids")
            if not isinstance(raw_many, list) or not raw_many:
                return None
            ids = [item for item in raw_many if isinstance(item, str) and item.strip()]
            if len(ids) != len(raw_many):
                return None
            return list(dict.fromkeys(ids))

        raw_one = tool_input.get("context_id")
        if isinstance(raw_one, str) and raw_one.strip():
            return [raw_one]
        return None

    @staticmethod
    def _format_retrieve_result(resolved: list[tuple[str, str, bool]]) -> str:
        if len(resolved) == 1:
            return resolved[0][1]
        return "\n\n".join(f"## {context_id}\n{content}" for context_id, content, _ in resolved)


class CurrentTurnWriteGuardMiddleware:
    """Replace unsafe write tool_use with a model-visible recovery message.

    V2.0 intentionally does not allow line-remap exceptions yet. The safe path is
    to retrieve the original context first, then retry the write operation.
    Do not use transport-level 4xx here: Claude CLI treats that as a retryable
    request failure and can loop on the same unsafe write attempt.
    """

    name = "current_turn_write_guard"

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        if not self._settings.compression_write_guard_enabled:
            await call_next(ctx)
            return
        current_ctx_ids = ctx.extras.get("current_turn_context_ids") or []
        if not current_ctx_ids or ctx.response_json is None:
            await call_next(ctx)
            return

        write_tools = _csv_names(self._settings.compression_write_guard_tools)
        content = ctx.response_json.get("content") or []
        write_blocks: list[tuple[str, dict[str, Any]]] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            if name.lower() not in write_tools:
                continue
            tool_input = block.get("input", {})
            if not isinstance(tool_input, dict):
                tool_input = {}
            write_blocks.append((name, tool_input))

        if not write_blocks:
            await call_next(ctx)
            return

        flags = ctx.extras.setdefault("feature_flags", {})
        flags["current_turn_write_guard_blocked"] = True
        flags["current_turn_write_guard_contexts"] = len(current_ctx_ids)
        flags["current_turn_write_guard_mode"] = "blocked"
        flags["current_turn_write_guard_tool"] = write_blocks[0][0]

        allowed_tools: list[str] = []
        blocked_tool = ""
        reason = "blocked_no_retrieve"
        for tool_name, tool_input in write_blocks:
            allowed, reason = self._allow_write_after_safety_check(
                ctx,
                tool_name,
                tool_input,
            )
            if not allowed:
                blocked_tool = tool_name
                flags["current_turn_write_guard_tool"] = tool_name
                flags["current_turn_write_guard_block_reason"] = reason
                break
            allowed_tools.append(tool_name)

        if not blocked_tool:
            flags["current_turn_write_guard_blocked"] = False
            flags["current_turn_write_guard_mode"] = reason
            flags["current_turn_write_guard_allow_tool"] = ",".join(allowed_tools)
            flags["current_turn_write_guard_block_reason"] = None
            await call_next(ctx)
            return

        ctx.blocked = False
        ctx.block_status_code = 200
        ctx.block_reason = "current_turn_compressed_context_write_guard"
        target_path = flags.get("current_turn_write_guard_target_path")
        message_context_ids = self._context_ids_for_guard_message(
            ctx, target_path if isinstance(target_path, str) else None
        )
        target_note = f" Target path: {target_path}." if isinstance(target_path, str) else ""
        message = (
            f"[CCIM] Blocked {blocked_tool} because this request used compressed "
            "current-turn context. Retrieve the original context first, then retry "
            f"the write.{target_note} Reason: {reason}. Context ids: "
            f"{', '.join(message_context_ids)}"
        )
        ctx.response_json = {
            **ctx.response_json,
            "content": [{"type": "text", "text": message}],
            "stop_reason": "end_turn",
        }
        await call_next(ctx)
        return

    def _allow_write_after_safety_check(
        self,
        ctx: RequestContext,
        blocked_tool: str,
        tool_input: dict[str, Any],
    ) -> tuple[bool, str]:
        target_path = _tool_input_path(tool_input)
        flags = ctx.extras.setdefault("feature_flags", {})
        flags["current_turn_write_guard_target_path"] = target_path
        if self._is_unrelated_write_target(ctx, target_path):
            return True, "allowed_unrelated_write"

        tool = blocked_tool.lower()
        if tool == "edit":
            return self._allow_edit_after_retrieve(ctx, tool_input, target_path)
        if tool == "multiedit":
            return self._allow_multiedit_after_retrieve(ctx, tool_input, target_path)
        if tool == "write":
            return self._allow_source_write_after_retrieve(ctx, target_path)
        return False, "unsupported_write_tool"

    def _allow_edit_after_retrieve(
        self,
        ctx: RequestContext,
        edit_input: dict[str, Any],
        target_path: str | None,
    ) -> tuple[bool, str]:
        old_string = edit_input.get("old_string")
        if not isinstance(old_string, str) or not old_string:
            return False, "missing_old_string"
        return self._allow_old_strings_after_retrieve(ctx, [old_string], target_path)

    def _allow_multiedit_after_retrieve(
        self,
        ctx: RequestContext,
        tool_input: dict[str, Any],
        target_path: str | None,
    ) -> tuple[bool, str]:
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return False, "missing_edits"
        old_strings: list[str] = []
        for edit in edits:
            if not isinstance(edit, dict):
                return False, "invalid_edit"
            old_string = edit.get("old_string")
            if not isinstance(old_string, str) or not old_string:
                return False, "missing_old_string"
            old_strings.append(old_string)
        return self._allow_old_strings_after_retrieve(ctx, old_strings, target_path)

    def _allow_old_strings_after_retrieve(
        self,
        ctx: RequestContext,
        old_strings: list[str],
        target_path: str | None,
    ) -> tuple[bool, str]:
        target_context_ids = self._current_turn_context_ids_for_path(ctx, target_path)
        current_ctx_ids = target_context_ids or set(
            ctx.extras.get("current_turn_context_ids") or []
        )
        retrieved = self._retrieved_contexts(ctx)
        retrieved_current = {
            context_id: original
            for context_id, original in retrieved.items()
            if context_id in current_ctx_ids
        }
        flags = ctx.extras.setdefault("feature_flags", {})
        if target_context_ids:
            flags["current_turn_write_guard_required_contexts"] = len(target_context_ids)
        elif (
            target_path
            and len(current_ctx_ids) > 1
            and self._source_mapping_incomplete(ctx)
        ):
            flags["current_turn_write_guard_required_contexts"] = len(current_ctx_ids)
            flags["current_turn_write_guard_unknown_source_contexts"] = len(
                self._unknown_source_context_ids(ctx)
            )
            flags["current_turn_write_guard_retrieved_contexts"] = 0
            flags["current_turn_write_guard_validated_contexts"] = 0
            return False, "blocked_target_context_unknown"
        flags["current_turn_write_guard_retrieved_contexts"] = len(retrieved_current)
        if not retrieved_current:
            return False, "blocked_no_retrieve"

        if all(self._old_string_in_retrieved(old, retrieved_current) for old in old_strings):
            validated_context_ids = {
                context_id
                for context_id, original in retrieved_current.items()
                if any(old in original for old in old_strings)
            }
            flags["current_turn_write_guard_validated_contexts"] = len(
                validated_context_ids
            )
            flags["current_turn_write_guard_validated_context_ids"] = sorted(
                validated_context_ids
            )
            return True, "allowed_after_retrieve"
        flags["current_turn_write_guard_validated_contexts"] = 0
        return False, "blocked_old_string_missing"

    @staticmethod
    def _old_string_in_retrieved(old_string: str, retrieved: dict[str, str]) -> bool:
        return any(old_string in original for original in retrieved.values())

    def _allow_source_write_after_retrieve(
        self, ctx: RequestContext, target_path: str | None
    ) -> tuple[bool, str]:
        target_context_ids = self._current_turn_context_ids_for_path(ctx, target_path)
        if not target_context_ids:
            return False, "blocked_source_write_unknown_context"
        retrieved = self._retrieved_contexts(ctx)
        retrieved_target = {
            context_id
            for context_id in target_context_ids
            if context_id in retrieved
        }
        flags = ctx.extras.setdefault("feature_flags", {})
        flags["current_turn_write_guard_retrieved_contexts"] = len(retrieved_target)
        flags["current_turn_write_guard_required_contexts"] = len(target_context_ids)
        if len(retrieved_target) == len(target_context_ids):
            flags["current_turn_write_guard_validated_contexts"] = len(retrieved_target)
            return True, "allowed_after_retrieve"
        return False, "blocked_incomplete_retrieve"

    def _is_unrelated_write_target(
        self, ctx: RequestContext, target_path: str | None
    ) -> bool:
        if not target_path:
            return False
        source_paths = self._current_turn_source_paths(ctx)
        if self._unknown_source_context_ids(ctx):
            return False
        if not source_paths:
            return False
        return target_path not in source_paths

    @staticmethod
    def _current_turn_source_paths(ctx: RequestContext) -> set[str]:
        raw = ctx.extras.get("current_turn_source_paths") or set()
        if isinstance(raw, set):
            return {_normalize_tool_path(path) for path in raw if isinstance(path, str)}
        if isinstance(raw, list):
            return {_normalize_tool_path(path) for path in raw if isinstance(path, str)}
        return set()

    @staticmethod
    def _current_turn_context_ids_for_path(
        ctx: RequestContext, target_path: str | None
    ) -> set[str]:
        if not target_path:
            return set()
        sources = ctx.extras.get("current_turn_context_sources") or {}
        if not isinstance(sources, dict):
            return set()
        return {
            context_id
            for context_id, source_path in sources.items()
            if isinstance(context_id, str)
            and isinstance(source_path, str)
            and _normalize_tool_path(source_path) == target_path
        }

    @staticmethod
    def _unknown_source_context_ids(ctx: RequestContext) -> set[str]:
        raw_context_ids = ctx.extras.get("current_turn_context_ids") or []
        if not isinstance(raw_context_ids, list):
            return set()
        sources = ctx.extras.get("current_turn_context_sources") or {}
        if not isinstance(sources, dict):
            return {item for item in raw_context_ids if isinstance(item, str)}
        return {
            context_id
            for context_id in raw_context_ids
            if isinstance(context_id, str) and context_id not in sources
        }

    def _source_mapping_incomplete(self, ctx: RequestContext) -> bool:
        return bool(self._unknown_source_context_ids(ctx))

    def _context_ids_for_guard_message(
        self, ctx: RequestContext, target_path: str | None
    ) -> list[str]:
        path_contexts = self._current_turn_context_ids_for_path(ctx, target_path)
        if path_contexts:
            return sorted(path_contexts)
        return sorted(ctx.extras.get("current_turn_context_ids") or [])

    def _retrieved_contexts(self, ctx: RequestContext) -> dict[str, str]:
        retrieved: dict[str, str] = {}
        from_extras = ctx.extras.get("retrieved_contexts") or {}
        if isinstance(from_extras, dict):
            for context_id, content in from_extras.items():
                if isinstance(context_id, str) and isinstance(content, str):
                    retrieved[context_id] = content
        retrieved.update(self._retrieved_contexts_from_history(ctx.request.messages))
        return retrieved

    @staticmethod
    def _retrieved_contexts_from_history(messages: list[Message]) -> dict[str, str]:
        tool_contexts: dict[str, str] = {}
        retrieved: dict[str, str] = {}
        for msg in messages:
            if not isinstance(msg.content, list):
                continue
            for block in msg.content:
                if (
                    isinstance(block, ToolUseBlock)
                    and block.name == _RETRIEVE_TOOL_NAME
                    and isinstance(block.input.get("context_id"), str)
                ):
                    tool_contexts[block.id] = block.input["context_id"]
                elif isinstance(block, ToolResultBlock) and not block.is_error:
                    context_id = tool_contexts.get(block.tool_use_id)
                    if context_id:
                        retrieved[context_id] = _tool_result_content_text(block)
        return retrieved


# ─────────────────────────────────────────────────────────────────────
# Stage 4 — WriteRemap
# ─────────────────────────────────────────────────────────────────────


class WriteRemapMiddleware:
    """설계 §4.3 — 편집 도구 라인 번호 역변환."""

    name = "write_remap"

    def __init__(self, mapper: Any) -> None:  # WriteMapper
        self._mapper = mapper

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        from ccim.write_mapper.mapper import has_line_args

        if ctx.response_json is None:
            await call_next(ctx)
            return

        content = ctx.response_json.get("content") or []
        for i, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_name = block.get("name", "")
            if not has_line_args(tool_name):
                continue

            tool_input = block.get("input", {})

            # context_id 결정 순서:
            # 1) tool_input에 명시된 context_id
            # 2) 단일 컨텍스트 요청인 경우 extras["active_context_id"] (fallback)
            # 3) 다중 컨텍스트 요청에서 context_id 미명시 → 안전하게 건너뜀
            raw_ctx = tool_input.get("context_id") or ctx.extras.get("active_context_id", "")
            if not raw_ctx:
                all_ctx = ctx.extras.get("all_context_ids", [])
                if len(all_ctx) > 1:
                    _logger.warning(
                        "[write_remap] context_id 미명시 + 다중 컨텍스트(%d개) — remap 건너뜀",
                        len(all_ctx),
                    )
                continue
            if ":" not in raw_ctx:
                continue

            session_id, _, context_id = raw_ctx.partition(":")

            new_input, results = await self._mapper.remap_tool_use(
                session_id=session_id,
                context_id=context_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )

            failures = [r for r in results if not r.ok]
            if failures:
                # miss 시 차단 (설계 §6 정책)
                content[i] = {
                    "type": "text",
                    "text": (
                        f"[CCIM] 라인 매핑 오류: {failures[0].error}. "
                        "retrieve_original을 먼저 호출하거나 원본 코드로 재시도하세요."
                    ),
                }
                ctx.response_json["content"] = content
            else:
                content[i]["input"] = new_input
                ctx.response_json["content"] = content
                ctx.write_remaps += 1

        await call_next(ctx)


# ─────────────────────────────────────────────────────────────────────
# Stage 5 — Telemetry
# ─────────────────────────────────────────────────────────────────────


class TelemetryMiddleware:
    """설계 §3.3 — PostgreSQL INSERT (fire-and-forget)."""

    name = "telemetry"

    def __init__(self, logger: Any) -> None:  # RequestLogger
        self._logger = logger
        self._pending_tasks: set[Any] = set()

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        await call_next(ctx)
        # 응답 경로를 막지 않도록 background task로 기록한다.
        self._schedule_log(ctx)

    def _schedule_log(self, ctx: RequestContext) -> None:
        import asyncio

        task = asyncio.create_task(self._fire_and_forget(ctx))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _fire_and_forget(self, ctx: RequestContext) -> None:
        from ccim.telemetry.logger import RequestRecord

        total_ms = sum(ctx.timings_ms.values())
        record = RequestRecord(
            session_id=ctx.session_id,
            pcfi_action=ctx.pcfi_action or "unknown",
            pcfi_reason=ctx.pcfi_reason,
            tokens_input_original=ctx.tokens_input_original,
            tokens_input_compressed=ctx.tokens_input_compressed,
            tokens_output=ctx.tokens_output,
            latency_ms=total_ms,
            pcfi_latency_ms=ctx.timings_ms.get("pcfi"),
            compress_latency_ms=ctx.timings_ms.get("compress"),
            upstream_latency_ms=ctx.timings_ms.get("forward"),
            retrieve_original_calls=ctx.retrieve_original_calls,
            write_remaps=ctx.write_remaps,
            feature_flags=ctx.extras.get("feature_flags", {}),
        )
        # 실패해도 메인 응답 경로에 영향 없음
        with suppress(Exception):
            await self._logger.log(record)


# ─────────────────────────────────────────────────────────────────────
# Stage 5 — OrphanMarkerScan
# ─────────────────────────────────────────────────────────────────────


class OrphanMarkerScanMiddleware:
    """LLM 응답 텍스트에 남은 고아 마커를 Redis에서 원본 복원 후 치환.

    압축 레이어가 정상 동작하면 이 단계에서 처리할 마커는 없다.
    LLM이 압축된 컨텍스트를 그대로 에코하거나, 드물게 복원 루프를
    거치지 않은 마커가 응답에 포함될 때 최종 안전망 역할을 한다.

    복원 성공 → 원본 코드로 치환
    복원 실패 → "[CCIM: 복원 불가 ...]" 표시 (조용한 오염 방지)
    """

    name = "marker_scan"

    def __init__(self, store: Any) -> None:
        self._store = store  # ReversibilityStore

    async def __call__(self, ctx: RequestContext, call_next: NextCallable) -> None:
        await call_next(ctx)

        if ctx.response_json is None:
            return

        content = ctx.response_json.get("content") or []
        modified = False

        for i, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            if "<<CTX_" not in text:
                continue  # 빠른 skip

            new_text = await self._restore_markers(text)
            if new_text is not text:
                content[i] = {"type": "text", "text": new_text}
                modified = True

        if modified:
            ctx.response_json = {**ctx.response_json, "content": content}

    async def _restore_markers(self, text: str) -> str:
        """텍스트 내 모든 마커를 역순으로 치환해 인덱스를 보존."""
        result = text
        for m in reversed(list(_ORPHAN_MARKER_RE.finditer(text))):
            session_id = m.group("session")
            ctx_id = m.group("ctx")
            try:
                record = await self._store.get(session_id, ctx_id)
            except Exception:
                record = None

            replacement = (
                record.original_code
                if record is not None                else "[CCIM: orphan marker - restore failed]"
            )
            result = result[: m.start()] + replacement + result[m.end() :]
        return result


# ─────────────────────────────────────────────────────────────────────
# SSE 합성 유틸
# ─────────────────────────────────────────────────────────────────────


async def response_dict_to_sse(response: dict[str, Any]) -> AsyncIterator[bytes]:
    """non-stream complete 결과를 Anthropic SSE 포맷으로 변환."""
    from ccim.llm.translate import encode_sse_event

    msg_id = response.get("id", f"msg_{uuid.uuid4().hex[:16]}")
    model = response.get("model", "")
    content = response.get("content") or []
    usage = response.get("usage") or {}
    stop_reason = response.get("stop_reason", "end_turn")

    yield encode_sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
            },
        },
    )

    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "")
            yield encode_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield encode_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
            yield encode_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": idx}
            )

        elif btype == "tool_use":
            yield encode_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": {},
                    },
                },
            )
            yield encode_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            block.get("input", {}), ensure_ascii=False
                        ),
                    },
                },
            )
            yield encode_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": idx}
            )

    yield encode_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        },
    )
    yield encode_sse_event("message_stop", {"type": "message_stop"})
