"""트리거 휴리스틱 -- 언제 압축을 시작할지 결정 (설계 3.2.5).

압축 탐지 대상:
  - TextBlock   : ```python / ```java / ```csharp 코드 펜스
  - ToolResultBlock : 파일 읽기(Read 도구) 결과 raw 코드
    Claude Code Read 도구는 줄 번호를 'N\\t' 접두사로 붙임.
    줄 번호 제거 후 분석/압축해야 ASTCompressor가 올바르게 동작.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ccim.api.schemas import Message, TextBlock, ToolResultBlock
from ccim.compress.structured_outputs import is_structured_output_candidate
from ccim.utils.tokens import estimate_message_tokens

# 지원 언어 펜스 태그 (그룹 1 = 언어 태그, 그룹 2 = 코드 본문)
_CODE_FENCE_RE = re.compile(
    r"```(python|py|java|csharp|c#|cs)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_MIN_CODE_LINES = 5
_MIN_RAW_CODE_LINES = 15
_BODY_FRACTION_GUESS = 0.5

_LINE_NUM_PREFIX_RE = re.compile(r"^\d+\t", re.MULTILINE)

# 언어별 코드 힌트 정규식 (ToolResultBlock raw 코드 언어 감지용)
_PYTHON_HINT_RE = re.compile(
    r"^(?:\d+\t)?\s*(?:def |class |async def )", re.MULTILINE
)
_JAVA_HINT_RE = re.compile(
    r"^(?:\d+\t)?\s*(?:public|private|protected|static)\s+[\w<>\[\]]+\s+\w+\s*\(",
    re.MULTILINE,
)
_CS_HINT_RE = re.compile(
    r"^(?:\d+\t)?\s*(?:public|private|protected|internal|static|virtual|override|abstract)\s+[\w<>\[\]?]+\s+\w+\s*\(",
    re.MULTILINE,
)

# 언어 별칭 → 정규화 키 (ast_compressor.LANG_ALIASES와 동일)
_LANG_ALIASES: dict[str, str] = {
    "python": "python", "py": "python",
    "java":   "java",
    "csharp": "csharp", "c#": "csharp", "cs": "csharp",
}


@dataclass(frozen=True)
class CompressionDiagnostics:
    total_tokens: int
    threshold_tokens: int
    target_tokens: int
    last_user_idx: int
    total_messages: int
    eligible_messages: int
    compressible_messages: int
    selected_messages: int
    current_turn_excluded: int
    system_excluded: int
    no_content_messages: int
    skip_reason: str | None

    def as_feature_flags(self) -> dict[str, int | str | None]:
        return {
            "compress_total_tokens": self.total_tokens,
            "compress_threshold_tokens": self.threshold_tokens,
            "compress_target_tokens": self.target_tokens,
            "compress_last_user_idx": self.last_user_idx,
            "compress_total_messages": self.total_messages,
            "compress_eligible_messages": self.eligible_messages,
            "compress_compressible_messages": self.compressible_messages,
            "compress_selected_messages": self.selected_messages,
            "compress_current_turn_excluded": self.current_turn_excluded,
            "compress_system_excluded": self.system_excluded,
            "compress_no_content_messages": self.no_content_messages,
            "compress_skip_reason": self.skip_reason,
        }


def _strip_line_numbers(text: str) -> str:
    """'N\\t' 형식 줄 번호 접두사 제거.

    Claude Code Read 도구는 파일 내용을 '줄번호\\t내용' 형식으로 반환한다.
    앞 5줄 중 3줄 이상이 N\\t 형식이면 번호 있는 것으로 판단.
    """
    sample = text.split("\n", 10)
    numbered = sum(1 for ln in sample[:5] if _LINE_NUM_PREFIX_RE.match(ln))
    if numbered >= 3:
        return _LINE_NUM_PREFIX_RE.sub("", text)
    return text


def detect_language_from_fence(fence_tag: str | None) -> str:
    """코드 펜스 언어 태그를 정규화된 언어 키로 변환. 없으면 'python'."""
    if not fence_tag:
        return "python"
    return _LANG_ALIASES.get(fence_tag.lower().strip(), "python")


def detect_language_from_code(code: str) -> str:
    """raw 코드에서 언어를 추측. 판별 불가 시 'python' 반환.

    우선순위: Java 고유 패턴 → C# 고유 패턴 → Python 기본값.
    """
    stripped = _strip_line_numbers(code)
    # Java: 확정적 마커
    if re.search(r"^package\s+[\w.]+;", stripped, re.MULTILINE):
        return "java"
    if re.search(r"@Override|@SuppressWarnings|System\.out\.", stripped):
        return "java"
    # C#: 확정적 마커 — hint RE보다 먼저 검사 (namespace/using은 Java에 없음)
    if re.search(r"^namespace\s+\w+", stripped, re.MULTILINE):
        return "csharp"
    if re.search(r"^using\s+[\w.]+;", stripped, re.MULTILINE):
        return "csharp"
    # 휴리스틱 fallback
    if _JAVA_HINT_RE.search(stripped) and not _PYTHON_HINT_RE.search(stripped):
        return "java"
    if _CS_HINT_RE.search(stripped) and not _PYTHON_HINT_RE.search(stripped):
        return "csharp"
    return "python"


def should_compress(
    messages: Iterable[Message],
    *,
    threshold_tokens: int,
    target_tokens: int,
    estimate_tokens_fn: Callable[[Message], int] | None = None,
    request_tokens: int | None = None,
) -> list[Message]:
    """압축 후보 메시지 리스트 반환. 임계치 미만이면 빈 리스트."""
    selected, _ = select_compression_candidates(
        messages,
        threshold_tokens=threshold_tokens,
        target_tokens=target_tokens,
        estimate_tokens_fn=estimate_tokens_fn,
        request_tokens=request_tokens,
    )
    return selected


def select_compression_candidates(
    messages: Iterable[Message],
    *,
    threshold_tokens: int,
    target_tokens: int,
    estimate_tokens_fn: Callable[[Message], int] | None = None,
    request_tokens: int | None = None,
) -> tuple[list[Message], CompressionDiagnostics]:
    """Return candidates plus detailed trigger diagnostics for telemetry."""
    msgs = list(messages)
    estimator = estimate_tokens_fn or estimate_message_tokens
    total = request_tokens if request_tokens is not None else sum(estimator(m) for m in msgs)
    last_user_idx = -1
    for i, m in enumerate(msgs):
        if m.role == "user":
            last_user_idx = i

    if total < threshold_tokens:
        return [], CompressionDiagnostics(
            total_tokens=total,
            threshold_tokens=threshold_tokens,
            target_tokens=target_tokens,
            last_user_idx=last_user_idx,
            total_messages=len(msgs),
            eligible_messages=0,
            compressible_messages=sum(1 for m in msgs if has_compressible_content(m)),
            selected_messages=0,
            current_turn_excluded=0,
            system_excluded=0,
            no_content_messages=0,
            skip_reason="below_threshold",
        )

    current_turn_excluded = 0
    system_excluded = 0
    no_content_messages = 0
    eligible_messages = 0
    compressible_messages = 0

    target_reduction = max(0, total - target_tokens)
    selected: list[Message] = []
    reduced = 0
    for i, m in enumerate(msgs):
        if reduced >= target_reduction:
            break
        if last_user_idx >= 0 and i >= last_user_idx:
            if has_compressible_content(m):
                current_turn_excluded += 1
            continue
        if m.role == "system":
            if has_compressible_content(m):
                system_excluded += 1
            continue
        eligible_messages += 1
        if not has_compressible_content(m):
            no_content_messages += 1
            continue
        compressible_messages += 1
        savings = int(estimator(m) * _BODY_FRACTION_GUESS)
        selected.append(m)
        reduced += savings

    skip_reason = None
    if not selected:
        if target_reduction <= 0:
            skip_reason = "target_already_met"
        elif compressible_messages == 0 and current_turn_excluded > 0:
            skip_reason = "current_turn_excluded"
        elif compressible_messages == 0 and system_excluded > 0:
            skip_reason = "system_excluded"
        elif eligible_messages == 0:
            skip_reason = "no_eligible_messages"
        elif compressible_messages == 0:
            skip_reason = "no_compressible_content"
        else:
            skip_reason = "not_selected"

    diagnostics = CompressionDiagnostics(
        total_tokens=total,
        threshold_tokens=threshold_tokens,
        target_tokens=target_tokens,
        last_user_idx=last_user_idx,
        total_messages=len(msgs),
        eligible_messages=eligible_messages,
        compressible_messages=compressible_messages,
        selected_messages=len(selected),
        current_turn_excluded=current_turn_excluded,
        system_excluded=system_excluded,
        no_content_messages=no_content_messages,
        skip_reason=skip_reason,
    )
    return selected, diagnostics


def is_current_turn(message: Message, all_messages: Iterable[Message]) -> bool:
    """가장 최근 user 발화 이후의 메시지면 True."""
    msgs = list(all_messages)
    last_user_idx = -1
    for i, m in enumerate(msgs):
        if m.role == "user":
            last_user_idx = i
    if last_user_idx < 0:
        return False
    for i, m in enumerate(msgs):
        if m is message:
            return i >= last_user_idx
    return False


def has_compressible_code(message: Message) -> bool:
    """메시지에 압축 가능한 코드(Python/Java/C#)가 있으면 True."""
    text = _extract_text(message)
    if text:
        for match in _CODE_FENCE_RE.finditer(text):
            body = match.group(2)
            if body.count("\n") >= _MIN_CODE_LINES:
                return True
    return _has_raw_code_tool_result(message)


def has_compressible_content(message: Message) -> bool:
    """Return True for code or conservative structured command output candidates."""
    if has_compressible_code(message):
        return True
    if not isinstance(message.content, list):
        return False
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        raw = _get_tool_result_text(block)
        if raw and is_structured_output_candidate(raw):
            return True
    return False


def is_raw_code_tool_result(block: ToolResultBlock) -> bool:
    """Return True if a ToolResultBlock looks like raw code worth AST compression."""
    raw = _get_tool_result_text(block)
    if not raw:
        return False
    stripped = _strip_line_numbers(raw)
    if stripped.count("\n") < _MIN_RAW_CODE_LINES:
        return False
    return bool(
        _PYTHON_HINT_RE.search(stripped)
        or _JAVA_HINT_RE.search(stripped)
        or _CS_HINT_RE.search(stripped)
    )


def _has_raw_code_tool_result(message: Message) -> bool:
    """ToolResultBlock에 raw 코드(Python/Java/C#)가 있으면 True."""
    if not isinstance(message.content, list):
        return False
    for block in message.content:
        if not isinstance(block, ToolResultBlock):
            continue
        if is_raw_code_tool_result(block):
            return True
    return False


def get_tool_result_text(block: ToolResultBlock) -> str:
    """ToolResultBlock에서 텍스트 추출 (chain.py 사용).

    줄 번호 접두사를 제거한 순수 코드를 반환 -- ASTCompressor 입력용.
    """
    raw = _get_tool_result_text(block)
    return _strip_line_numbers(raw)


def _get_tool_result_text(block: ToolResultBlock) -> str:
    """ToolResultBlock 원본 텍스트 추출 (줄 번호 미제거)."""
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


def _extract_text(message: Message) -> str:
    """TextBlock만 추출 (ToolResultBlock 제외)."""
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts)
