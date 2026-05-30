"""Intercept LLM tool_use(retrieve_original) calls -> resolve from Redis.

Design section 4.2 reversibility path:
  1. LLM emits tool_use(retrieve_original, context_id="session:ctx").
  2. Gateway intercepts; does NOT forward to the agent.
  3. Looks up the original code in Redis.
  4. Sends a tool_result back to the LLM and resumes generation.
  5. Final response (without the tool_use loop) is what the agent sees.

This module is the resolver. Driving the multi-turn loop with the upstream LLM
is the gateway/middleware's responsibility (see middleware/chain.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from ccim.reversibility.store import ReversibilityStore

RETRIEVE_TOOL_NAME = "retrieve_original"


@dataclass
class InterceptStats:
    retrieve_calls: int = 0
    retrieve_hits: int = 0
    retrieve_misses: int = 0


@dataclass
class ToolResolution:
    """Outcome of resolving a single retrieve_original tool_use."""

    content: str
    is_error: bool


class ReversibilityInterceptor:
    """Tool-use resolver. Stateless aside from per-instance stats."""

    def __init__(self, store: ReversibilityStore, max_loops: int = 5) -> None:
        self._store = store
        self._max_loops = max_loops
        self._stats = InterceptStats()

    @property
    def stats(self) -> InterceptStats:
        return self._stats

    @property
    def max_loops(self) -> int:
        """Cap on retrieve_original tool_use turns per request (loop guard)."""
        return self._max_loops

    @staticmethod
    def is_retrieve_call(name: str) -> bool:
        return name == RETRIEVE_TOOL_NAME

    async def handle_tool_use(
        self,
        tool_input: dict,
        *,
        expected_session_id: str | None = None,
    ) -> ToolResolution:
        """Resolve a retrieve_original tool input.

        Tool input shape:  {"context_id": "session:ctx"}
        Returns text suitable for a tool_result content + an is_error flag.
        Errors are surfaced as text (not exceptions) so the LLM can retry or recover.
        """
        ctx = tool_input.get("context_id", "")
        if not isinstance(ctx, str) or ":" not in ctx:
            self._stats.retrieve_calls += 1
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=f"error: invalid context_id format: {ctx!r}", is_error=True
            )

        session_id, _, context_id = ctx.partition(":")
        self._stats.retrieve_calls += 1
        if expected_session_id is not None and session_id != expected_session_id:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=(
                    "error: context_id belongs to a different session. "
                    "Use only context ids created in this request session."
                ),
                is_error=True,
            )
        record = await self._store.get(session_id, context_id)
        if record is None:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=(
                    f"error: context not found or expired: {ctx}. "
                    "Do not fabricate the body; ask the user to re-paste the code."
                ),
                is_error=True,
            )

        self._stats.retrieve_hits += 1
        return ToolResolution(content=record.original_code, is_error=False)
