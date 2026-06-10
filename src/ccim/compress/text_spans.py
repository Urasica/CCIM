"""Conservative text evidence span compression.

This module handles non-code evidence such as logs, email-like threads, and
Markdown/plain text sections. It does not interpret the evidence; it only keeps
bounded spans recoverable through CCIM context markers.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from ccim.compress.markers import build_marker
from ccim.reversibility.store import compute_document_hash
from ccim.utils.tokens import estimate_text_tokens

MIN_TEXT_SPAN_CHARS = 1200
MIN_TEXT_SPAN_LINES = 20
MAX_LOG_CHUNK_LINES = 80
MAX_DOCUMENT_CHUNK_CHARS = 5000

_LOG_TS_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
_LOG_LEVEL_RE = re.compile(r"\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\b")
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_EMAIL_HEADER_RE = re.compile(r"^(?:From|To|Cc|Date|Subject):\s+.+$", re.MULTILINE)


@dataclass(frozen=True)
class TextSpanBlock:
    context_id: str
    marker: str
    original_code: str
    original_lines: tuple[int, int]
    span_type: str
    source_kind: str
    source_uri: str | None = None
    symbol_name: str | None = None
    document_id: str | None = None
    document_hash: str | None = None
    document_version: int = 1
    metadata: dict[str, str | int] = field(default_factory=dict)


@dataclass
class TextSpanCompressionResult:
    compressed_text: str
    blocks: list[TextSpanBlock] = field(default_factory=list)
    line_mapping: dict[int, int] = field(default_factory=dict)
    tokens_before_est: int = 0
    tokens_after_est: int = 0

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before_est - self.tokens_after_est)


@dataclass(frozen=True)
class _SpanCandidate:
    start_line: int
    end_line: int
    text: str
    span_type: str
    source_kind: str
    title: str | None = None


def is_evidence_text_candidate(text: str, *, source_path: str | None = None) -> bool:
    """Return True when text is a conservative non-code evidence candidate."""
    if len(text) < MIN_TEXT_SPAN_CHARS or text.count("\n") + 1 < MIN_TEXT_SPAN_LINES:
        return False
    return detect_source_kind(text, source_path=source_path) is not None


def detect_source_kind(text: str, *, source_path: str | None = None) -> str | None:
    path = (source_path or "").lower()
    if path.endswith((".log", ".out", ".trace")):
        return "log"
    if path.endswith((".eml", ".mbox")):
        return "email"
    if path.endswith((".md", ".markdown", ".txt")):
        return "document"

    log_signal = len(_LOG_TS_RE.findall(text)) >= 5 and len(_LOG_LEVEL_RE.findall(text)) >= 5
    if log_signal:
        return "log"
    if len(_EMAIL_HEADER_RE.findall(text)) >= 4:
        return "email"
    if len(_MARKDOWN_HEADING_RE.findall(text)) >= 2:
        return "document"
    return None


def compress_text_spans(
    text: str,
    *,
    session_id: str,
    source_path: str | None = None,
    ctx_prefix: str | None = None,
    document_id: str | None = None,
    document_version: int = 1,
) -> TextSpanCompressionResult:
    source_kind = detect_source_kind(text, source_path=source_path)
    if source_kind is None:
        return TextSpanCompressionResult(
            compressed_text=text,
            tokens_before_est=estimate_text_tokens(text),
            tokens_after_est=estimate_text_tokens(text),
        )

    candidates = _span_candidates(text, source_kind=source_kind)
    if not candidates:
        return TextSpanCompressionResult(
            compressed_text=text,
            tokens_before_est=estimate_text_tokens(text),
            tokens_after_est=estimate_text_tokens(text),
        )

    prefix = ctx_prefix or uuid.uuid4().hex[:8]
    document_hash = compute_document_hash(text)
    effective_document_id = document_id or source_path or document_hash[:16]
    lines = text.splitlines(keepends=True)
    line_starts = _line_start_offsets(lines)
    replacements: list[tuple[int, int, str]] = []
    blocks: list[TextSpanBlock] = []

    for index, candidate in enumerate(candidates, start=1):
        context_id = f"{prefix}{index:03d}"
        marker = build_marker(session_id, context_id)
        replacement = _replacement_text(candidate, marker)
        start_offset = line_starts[candidate.start_line - 1]
        end_offset = line_starts[candidate.end_line]
        replacements.append((start_offset, end_offset, replacement))
        blocks.append(
            TextSpanBlock(
                context_id=context_id,
                marker=marker,
                original_code=candidate.text,
                original_lines=(candidate.start_line, candidate.end_line),
                span_type=candidate.span_type,
                source_kind=candidate.source_kind,
                source_uri=source_path,
                symbol_name=candidate.title,
                document_id=effective_document_id,
                document_hash=document_hash,
                document_version=document_version,
                metadata={
                    "line_count": candidate.end_line - candidate.start_line + 1,
                    "char_count": len(candidate.text),
                },
            )
        )

    compressed_text = text
    for start, end, replacement in reversed(replacements):
        compressed_text = compressed_text[:start] + replacement + compressed_text[end:]

    return TextSpanCompressionResult(
        compressed_text=compressed_text,
        blocks=blocks,
        line_mapping={},
        tokens_before_est=estimate_text_tokens(text),
        tokens_after_est=estimate_text_tokens(compressed_text),
    )


def _span_candidates(text: str, *, source_kind: str) -> list[_SpanCandidate]:
    if source_kind == "log":
        return _log_candidates(text)
    if source_kind == "email":
        return _email_candidates(text)
    return _document_candidates(text)


def _log_candidates(text: str) -> list[_SpanCandidate]:
    lines = text.splitlines(keepends=True)
    candidates = []
    for start in range(0, len(lines), MAX_LOG_CHUNK_LINES):
        chunk = lines[start : start + MAX_LOG_CHUNK_LINES]
        body = "".join(chunk)
        if _large_enough(body):
            candidates.append(
                _SpanCandidate(
                    start_line=start + 1,
                    end_line=start + len(chunk),
                    text=body,
                    span_type="log_window",
                    source_kind="log",
                    title=_log_title(body),
                )
            )
    return candidates


def _email_candidates(text: str) -> list[_SpanCandidate]:
    # P2 keeps email conservative: use document-sized chunks until a dedicated
    # thread parser exists.
    return [
        _SpanCandidate(
            start_line=start,
            end_line=end,
            text=body,
            span_type="email_message",
            source_kind="email",
            title=_first_matching_line(body, _EMAIL_HEADER_RE),
        )
        for start, end, body in _fixed_size_line_chunks(text)
        if _large_enough(body)
    ]


def _document_candidates(text: str) -> list[_SpanCandidate]:
    heading_matches = list(_MARKDOWN_HEADING_RE.finditer(text))
    if not heading_matches:
        return [
            _SpanCandidate(
                start_line=start,
                end_line=end,
                text=body,
                span_type="document_section",
                source_kind="document",
                title=None,
            )
            for start, end, body in _fixed_size_line_chunks(text)
            if _large_enough(body)
        ]

    candidates = []
    for i, match in enumerate(heading_matches):
        start = match.start()
        end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
        body = text[start:end]
        if _large_enough(body):
            candidates.append(
                _SpanCandidate(
                    start_line=_line_number_at(text, start),
                    end_line=_line_number_at(text, max(start, end - 1)),
                    text=body,
                    span_type="document_section",
                    source_kind="document",
                    title=match.group(0).strip("# ").strip(),
                )
            )
    return candidates


def _fixed_size_line_chunks(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines(keepends=True)
    chunks = []
    start = 0
    while start < len(lines):
        end = start
        chars = 0
        while end < len(lines) and chars < MAX_DOCUMENT_CHUNK_CHARS:
            chars += len(lines[end])
            end += 1
        body = "".join(lines[start:end])
        chunks.append((start + 1, end, body))
        start = end
    return chunks


def _replacement_text(candidate: _SpanCandidate, marker: str) -> str:
    title = f" title={candidate.title!r}" if candidate.title else ""
    line_count = candidate.end_line - candidate.start_line + 1
    return (
        f"[CCIM evidence span: {candidate.span_type}{title} "
        f"lines={candidate.start_line}-{candidate.end_line} "
        f"chars={len(candidate.text)} line_count={line_count}]\n"
        f"{marker}\n"
    )


def _line_start_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    total = 0
    for line in lines:
        total += len(line)
        offsets.append(total)
    return offsets


def _line_number_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _large_enough(text: str) -> bool:
    return len(text) >= MIN_TEXT_SPAN_CHARS and text.count("\n") + 1 >= MIN_TEXT_SPAN_LINES


def _log_title(text: str) -> str | None:
    for line in text.splitlines():
        if _LOG_LEVEL_RE.search(line):
            return line[:120]
    return None


def _first_matching_line(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    return match.group(0)[:120] if match else None
