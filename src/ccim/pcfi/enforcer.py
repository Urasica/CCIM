"""PCFI Enforcer - decompose into 4 compartments, then validate (design section 3.2.1).

Order:
  1. role-switch regex - quick block on 'ignore previous instructions' style hits in U/R
  2. Llama Guard - LLM-based risk classification on combined U+R
  Any positive hit -> immediate BLOCK.

V1 decisions:
  - SANITIZE remains in the enum but is unused (allow/block only).
  - If no Llama Guard is injected, regex-only is acceptable (test/local convenience).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from ccim.api.schemas import Message, TextBlock, ToolResultBlock
from ccim.pcfi.compartments import Compartments
from ccim.pcfi.llama_guard import GuardClient

logger = logging.getLogger(__name__)


class PCFIAction(StrEnum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


@dataclass
class PCFIVerdict:
    action: PCFIAction
    reason: str | None = None
    sanitized: Compartments | None = None
    latency_ms: int = 0


_ROLE_SWITCH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:ignore|disregard|forget)\s+"
        r"(?:all\s+)?(?:previous|prior|above|the|earlier)\s+"
        r"(?:instructions?|prompts?|rules?|messages?|context)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bforget\s+(?:everything|all\s+(?:that|of\s+the))\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*\[?\s*SYSTEM\s*\]?\s*:", re.IGNORECASE),
    # "### System" 단독은 차단, "### Instructions"는 일반 마크다운 헤더이므로 허용.
    # "### New Instructions" / "### New System" 처럼 'new'가 붙은 경우만 차단.
    re.compile(r"(?:^|\n)\s*###\s*(?:new\s+(?:system|instructions?)|system)\b", re.IGNORECASE),
    re.compile(r"<\|im_start\|>\s*system", re.IGNORECASE),
    re.compile(r"\[INST\][\s\S]{0,200}\[/INST\]", re.IGNORECASE),
    re.compile(
        r"\byou\s+are\s+now\s+(?:a|an)\s+"
        r"(?:helpful|harmful|different|new|unrestricted|jailbroken|dan|developer)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\brm\s+-rf\s+/(?!\s*['\"\`]?\s*(?:does?|is|are|would|will|can|could)\b)", re.IGNORECASE),
]


_GUARD_INPUT_MAX_CHARS = 12_000


class PCFIEnforcer:
    """V1 PCFI implementation: regex + optional Llama Guard."""

    def __init__(
        self,
        guard: GuardClient | None = None,
        skip_guard_categories: set[str] | None = None,
    ) -> None:
        self._guard = guard
        # 코딩 에이전트용: S14(Code Interpreter Abuse) 등 오탐이 잦은 카테고리를 skip.
        # 해당 카테고리만 탐지된 경우 block 대신 allow로 처리 (더 위험한 카테고리와 혼재하면 block 유지).
        self._skip_cats: set[str] = skip_guard_categories or set()

    async def check(self, compartments: Compartments) -> PCFIVerdict:
        start = time.perf_counter()

        reason = self._detect_role_switch(compartments)
        if reason:
            return PCFIVerdict(
                action=PCFIAction.BLOCK,
                reason=reason,
                latency_ms=_elapsed_ms(start),
            )

        guard_reason = await self._scan_with_guard(compartments)
        if guard_reason:
            return PCFIVerdict(
                action=PCFIAction.BLOCK,
                reason=guard_reason,
                latency_ms=_elapsed_ms(start),
            )

        return PCFIVerdict(action=PCFIAction.ALLOW, latency_ms=_elapsed_ms(start))

    def _detect_role_switch(self, compartments: Compartments) -> str | None:
        for section, msg in compartments.iter_scannable_messages():
            text = _extract_text(msg)
            if not text:
                continue
            for pattern in _ROLE_SWITCH_PATTERNS:
                m = pattern.search(text)
                if m:
                    snippet = m.group(0)[:60].replace("\n", " ")
                    return f"role_switch:{section.name}:{snippet!r}"
        return None

    async def _scan_with_guard(self, compartments: Compartments) -> str | None:
        if self._guard is None:
            return None
        conversation = _build_guard_input(compartments)
        if not conversation:
            return None
        try:
            result = await self._guard.classify(conversation)
        except httpx.ReadTimeout:
            logger.warning("Llama Guard timeout — falling back to allow (regex-only)")
            return None
        except Exception as exc:
            logger.warning("Llama Guard error (%s) — falling back to allow", exc)
            return None
        if result.safe:
            return None

        # skip 카테고리 필터링: 탐지된 카테고리 중 skip 대상이 아닌 것만 추려서 판단
        detected = set(result.categories)
        actionable = detected - self._skip_cats
        if not actionable:
            skipped = ",".join(sorted(detected))
            logger.info("Llama Guard: skipping allow-listed categories [%s]", skipped)
            return None

        cats = ",".join(sorted(actionable))
        return f"llama_guard:unsafe:{cats}"


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _extract_text(message: Message) -> str:
    """Concatenate TextBlock + ToolResultBlock text; skip ToolUseBlock."""
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            content = block.content
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def _build_guard_input(compartments: Compartments) -> str:
    """Join U+R text with [section] labels; cap length."""
    chunks: list[str] = []
    for section, msg in compartments.iter_scannable_messages():
        text = _extract_text(msg)
        if text:
            chunks.append(f"[{section.name}] {text}")
    joined = "\n\n".join(chunks)
    if len(joined) > _GUARD_INPUT_MAX_CHARS:
        joined = joined[-_GUARD_INPUT_MAX_CHARS:]
    return joined
