"""Translate edit-tool line numbers from compressed view to original (design 3.2.4 / 4.3).

Inputs come as tool_use payloads from the LLM (e.g. edit_file with line=42 in the
compressed file). The mapping was stored at compression time; we look it up by
context_id and rewrite the int line arguments in place.

V1 policy (per design section 6 risk row "edit line mapping breaks"):
  - One edit at a time. Multi-edit support is deferred.
  - On mapping miss: return MapResult(ok=False) and let the caller surface an
    error to the agent so the LLM can retry after retrieve_original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ccim.reversibility.store import ReversibilityStore


@dataclass
class MapResult:
    ok: bool
    original_line: int | None = None
    error: str | None = None


# Tool name -> argument keys that hold line numbers (1-based).
LINE_ARG_KEYS: dict[str, list[str]] = {
    "edit_file": ["line", "start_line", "end_line"],
    "apply_diff": ["line", "start_line", "end_line"],
    # search_replace: identifies lines via diff context, not by number -> skip.
    "search_replace": [],
}


def has_line_args(tool_name: str) -> bool:
    """True if Write Mapper should attempt to remap this tool's input."""
    return bool(LINE_ARG_KEYS.get(tool_name))


def translate_line_with_mapping(
    line_mapping: dict[int, int], compressed_line: int
) -> MapResult:
    """Pure helper: translate a single compressed line via a known mapping."""
    if compressed_line in line_mapping:
        return MapResult(ok=True, original_line=line_mapping[compressed_line])
    return MapResult(
        ok=False,
        error=(
            f"compressed_line={compressed_line} not in mapping "
            f"(known size={len(line_mapping)}). "
            "Call retrieve_original first or re-issue with an absolute original line."
        ),
    )


class WriteMapper:
    """Looks up document mapping in Redis and rewrites tool_use line args."""

    def __init__(self, store: ReversibilityStore) -> None:
        self._store = store

    async def translate_line(
        self, *, session_id: str, context_id: str, compressed_line: int
    ) -> MapResult:
        """Convenience: fetch mapping from store and translate one line."""
        line_mapping = await self._store.get_line_mapping(session_id, context_id)
        if line_mapping is None:
            return MapResult(
                ok=False,
                error=f"no mapping for ctx:{session_id}:{context_id} "
                "(expired or never stored)",
            )
        return translate_line_with_mapping(line_mapping, compressed_line)

    async def remap_tool_use(
        self,
        *,
        session_id: str,
        context_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[dict[str, Any], list[MapResult]]:
        """Return (rewritten_tool_input, per-key results).

        - Tools without line args (search_replace, retrieve_original, ...) are passed
          through untouched.
        - On mapping miss, the rewritten input is identical to the original and the
          first MapResult is the miss error; caller should reject the edit.
        """
        keys = LINE_ARG_KEYS.get(tool_name)
        if not keys:
            return dict(tool_input), []

        line_mapping = await self._store.get_line_mapping(session_id, context_id)
        if line_mapping is None:
            return dict(tool_input), [
                MapResult(
                    ok=False,
                    error=f"no mapping for ctx:{session_id}:{context_id}; "
                    "rejecting edit.",
                )
            ]

        new_input: dict[str, Any] = dict(tool_input)
        results: list[MapResult] = []
        for key in keys:
            if key not in new_input:
                continue
            raw = new_input[key]
            if not isinstance(raw, int) or isinstance(raw, bool):
                results.append(
                    MapResult(
                        ok=False,
                        error=f"{key!r} expected int, got {type(raw).__name__}: {raw!r}",
                    )
                )
                continue
            r = translate_line_with_mapping(line_mapping, raw)
            results.append(r)
            if r.ok and r.original_line is not None:
                new_input[key] = r.original_line
        return new_input, results
