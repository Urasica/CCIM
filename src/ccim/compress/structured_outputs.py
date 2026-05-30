"""Conservative compression helpers for structured command outputs."""

from __future__ import annotations

import hashlib
import re

MIN_TOOL_RESULT_DEDUPE_CHARS = 1200
_MIN_STRUCTURED_CHARS = 1200
_MAX_FAILURE_LINES = 80

_SUCCESS_PATTERNS = (
    re.compile(r"\bRan\s+\d+\s+tests?\s+in\s+[^\n]+\n\nOK\b"),
    re.compile(r"=+\s+\d+\s+passed(?:,\s+\d+\s+\w+)*\s+in\s+[^\n]+\s+=+"),
)
_STRUCTURED_HINTS = (
    re.compile(r"\bTraceback \(most recent call last\):"),
    re.compile(r"\b(Failed|FAILED|ERROR|Error|Exit code)\b"),
    re.compile(r"\b(FullyQualifiedErrorId|CategoryInfo)\b"),
    re.compile(r"=+\s+test session starts\s+=+"),
)
_KEEP_LINE_RE = re.compile(
    r"(Traceback \(most recent call last\)|^  File \"|^\s*\^+$|"
    r"\b(AssertionError|IndentationError|SyntaxError|TypeError|ValueError|RuntimeError):|"
    r"^(FAILED|ERROR|FAIL:|ERROR:)|\bExit code\s+\d+\b|"
    r"\b(FullyQualifiedErrorId|CategoryInfo)\b)"
)


def tool_result_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_dedupe_tool_result(text: str) -> bool:
    return len(text) >= MIN_TOOL_RESULT_DEDUPE_CHARS


def is_structured_output_candidate(text: str) -> bool:
    """Return True only for large, recognizable command/test outputs."""
    if len(text) < _MIN_STRUCTURED_CHARS:
        return False
    if any(p.search(text) for p in _SUCCESS_PATTERNS):
        return True
    return any(p.search(text) for p in _STRUCTURED_HINTS)


def build_tool_result_reference(content_hash: str, *, chars: int, lines: int) -> str:
    short = content_hash[:16]
    return (
        "[CCIM] Repeated tool_result omitted.\n"
        f"hash=sha256:{short} chars={chars} lines={lines}\n"
        "This exact output was already present earlier in this session."
    )


def summarize_command_output(text: str, *, is_error: bool = False) -> str | None:
    """Compress structured stdout/stderr while preserving the actionable result."""
    if not is_structured_output_candidate(text):
        return None

    lines = text.splitlines()
    content_hash = tool_result_hash(text)
    header = (
        "[CCIM] Structured command output compressed.\n"
        f"hash=sha256:{content_hash[:16]} chars={len(text)} lines={len(lines)}\n"
    )

    success = _extract_success_result(text)
    if success and not is_error:
        result = f"{header}\nResult:\n{success}"
        return result if len(result) < len(text) else None

    kept = _extract_failure_excerpt(lines)
    if not kept:
        kept = lines[-min(len(lines), _MAX_FAILURE_LINES):]

    omitted = max(0, len(lines) - len(kept))
    body = "\n".join(kept)
    result = (
        f"{header}\n"
        f"Diagnostic excerpt ({omitted} non-essential line(s) omitted):\n"
        f"{body}"
    )
    return result if len(result) < len(text) else None


def _extract_success_result(text: str) -> str | None:
    for pattern in _SUCCESS_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _extract_failure_excerpt(lines: list[str]) -> list[str]:
    selected: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if _KEEP_LINE_RE.search(line):
            selected.append((idx, line))
            if idx > 0:
                selected.append((idx - 1, lines[idx - 1]))
            if idx + 1 < len(lines):
                selected.append((idx + 1, lines[idx + 1]))

    if not selected:
        return []

    selected = sorted(set(selected), key=lambda item: item[0])
    if len(selected) > _MAX_FAILURE_LINES:
        selected = selected[-_MAX_FAILURE_LINES:]
    return [line for _, line in selected]
