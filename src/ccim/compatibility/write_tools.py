"""Deterministic write-tool schema registry used by safety middleware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WriteToolStatus = Literal["supported", "unsupported"]

_TOOL_KINDS = {
    "edit": "edit",
    "edit_file": "edit",
    "multiedit": "multiedit",
    "multi_edit": "multiedit",
    "write": "write",
    "write_file": "write",
    "apply_patch": "unsupported",
    "str_replace_editor": "unsupported",
}
_COMMON_KEYS = {"file_path", "path", "context_id"}
_ALLOWED_KEYS = {
    "edit": _COMMON_KEYS | {"old_string", "new_string", "replace_all"},
    "multiedit": _COMMON_KEYS | {"edits"},
    "write": _COMMON_KEYS | {"content"},
}


@dataclass(frozen=True)
class WriteToolInspection:
    status: WriteToolStatus
    tool_name: str
    canonical_name: str | None
    target_path: str | None
    old_strings: tuple[str, ...]
    reason: str


def write_tool_names() -> set[str]:
    """Return all known write-capable tool names in normalized form."""
    return set(_TOOL_KINDS)


def inspect_write_tool(
    tool_name: str,
    tool_input: Any,
) -> WriteToolInspection | None:
    """Validate one known write-tool call without executing or mutating it."""
    normalized_name = tool_name.strip().lower()
    kind = _TOOL_KINDS.get(normalized_name)
    if kind is None:
        return None
    if kind == "unsupported":
        return WriteToolInspection(
            status="unsupported",
            tool_name=tool_name,
            canonical_name=None,
            target_path=None,
            old_strings=(),
            reason="unsupported_write_tool",
        )
    if not isinstance(tool_input, dict):
        return _unsupported(tool_name, kind, "unsupported_write_schema")
    if set(tool_input) - _ALLOWED_KEYS[kind]:
        return _unsupported(tool_name, kind, "unsupported_write_schema")

    file_path = tool_input.get("file_path")
    path = tool_input.get("path")
    if (
        isinstance(file_path, str)
        and isinstance(path, str)
        and _normalize_path(file_path) != _normalize_path(path)
    ):
        return _unsupported(tool_name, kind, "ambiguous_write_target")
    raw_target = file_path or path
    if not isinstance(raw_target, str) or not raw_target.strip():
        return _unsupported(tool_name, kind, "unsupported_write_schema")
    target_path = _normalize_path(raw_target)

    if kind == "edit":
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        if (
            not isinstance(old_string, str)
            or not old_string
            or not isinstance(new_string, str)
        ):
            return _unsupported(tool_name, kind, "unsupported_write_schema")
        replace_all = tool_input.get("replace_all")
        if replace_all is not None and not isinstance(replace_all, bool):
            return _unsupported(tool_name, kind, "unsupported_write_schema")
        return WriteToolInspection(
            status="supported",
            tool_name=tool_name,
            canonical_name=kind,
            target_path=target_path,
            old_strings=(old_string,),
            reason="supported_write_schema",
        )

    if kind == "multiedit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return _unsupported(tool_name, kind, "unsupported_write_schema")
        old_strings: list[str] = []
        for edit in edits:
            if not isinstance(edit, dict):
                return _unsupported(tool_name, kind, "unsupported_write_schema")
            if set(edit) - {"old_string", "new_string", "replace_all"}:
                return _unsupported(tool_name, kind, "unsupported_write_schema")
            old_string = edit.get("old_string")
            new_string = edit.get("new_string")
            if (
                not isinstance(old_string, str)
                or not old_string
                or not isinstance(new_string, str)
            ):
                return _unsupported(tool_name, kind, "unsupported_write_schema")
            replace_all = edit.get("replace_all")
            if replace_all is not None and not isinstance(replace_all, bool):
                return _unsupported(tool_name, kind, "unsupported_write_schema")
            old_strings.append(old_string)
        return WriteToolInspection(
            status="supported",
            tool_name=tool_name,
            canonical_name=kind,
            target_path=target_path,
            old_strings=tuple(old_strings),
            reason="supported_write_schema",
        )

    content = tool_input.get("content")
    if not isinstance(content, str):
        return _unsupported(tool_name, kind, "unsupported_write_schema")
    return WriteToolInspection(
        status="supported",
        tool_name=tool_name,
        canonical_name=kind,
        target_path=target_path,
        old_strings=(),
        reason="supported_write_schema",
    )


def _unsupported(
    tool_name: str,
    canonical_name: str,
    reason: str,
) -> WriteToolInspection:
    return WriteToolInspection(
        status="unsupported",
        tool_name=tool_name,
        canonical_name=canonical_name,
        target_path=None,
        old_strings=(),
        reason=reason,
    )


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/").lower()
