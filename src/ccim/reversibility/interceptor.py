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
    persistent_store_hits: int = 0
    persistent_store_misses: int = 0
    redis_warm_loads: int = 0


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

        Tool input shape:  {"context_id": "session:ctx"} or
        {"context_ids": ["session:001", "session:002"]}.
        Returns text suitable for a tool_result content + an is_error flag.
        Errors are surfaced as text (not exceptions) so the LLM can retry or recover.
        """
        context_ids = self._context_ids_from_input(tool_input)
        self._stats.retrieve_calls += 1
        if not context_ids:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content="error: provide `context_id` or non-empty `context_ids`",
                is_error=True,
            )

        if len(context_ids) == 1 and "context_ids" not in tool_input:
            return await self._resolve_one(
                context_ids[0],
                expected_session_id=expected_session_id,
            )

        sections: list[str] = []
        any_error = False
        persistent_store_hits = 0
        persistent_store_misses = 0
        redis_warm_loads = 0
        for full_context_id in context_ids:
            resolution = await self._resolve_one(
                full_context_id,
                expected_session_id=expected_session_id,
            )
            any_error = any_error or resolution.is_error
            persistent_store_hits += resolution.persistent_store_hits
            persistent_store_misses += resolution.persistent_store_misses
            redis_warm_loads += resolution.redis_warm_loads
            sections.append(f"## {full_context_id}\n{resolution.content}")
        return ToolResolution(
            content="\n\n".join(sections),
            is_error=any_error,
            persistent_store_hits=persistent_store_hits,
            persistent_store_misses=persistent_store_misses,
            redis_warm_loads=redis_warm_loads,
        )

    @staticmethod
    def _context_ids_from_input(tool_input: dict) -> list[str]:
        raw_many = tool_input.get("context_ids")
        if raw_many is not None:
            if not isinstance(raw_many, list):
                return []
            ids = [item for item in raw_many if isinstance(item, str) and item.strip()]
            return list(dict.fromkeys(ids))

        raw_one = tool_input.get("context_id")
        if isinstance(raw_one, str) and raw_one.strip():
            return [raw_one]
        return []

    async def _resolve_one(
        self,
        full_context_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> ToolResolution:
        if ":" not in full_context_id:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=f"error: invalid context_id format: {full_context_id!r}",
                is_error=True,
            )

        session_id, _, context_id = full_context_id.partition(":")
        if expected_session_id is not None and session_id != expected_session_id:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=(
                    "error: context_id belongs to a different session. "
                    "Use only context ids created in this request session."
                ),
                is_error=True,
            )
        before = self._store_stats_snapshot()
        record = await self._store.get(session_id, context_id)
        delta = self._store_stats_delta(before)
        if record is None:
            self._stats.retrieve_misses += 1
            return ToolResolution(
                content=(
                    f"error: context not found or expired: {full_context_id}. "
                    "Do not fabricate the body; ask the user to re-paste the code."
                ),
                is_error=True,
                persistent_store_hits=delta["persistent_hits"],
                persistent_store_misses=delta["persistent_misses"],
                redis_warm_loads=delta["redis_warm_loads"],
            )

        self._stats.retrieve_hits += 1
        return ToolResolution(
            content=record.original_code,
            is_error=False,
            persistent_store_hits=delta["persistent_hits"],
            persistent_store_misses=delta["persistent_misses"],
            redis_warm_loads=delta["redis_warm_loads"],
        )

    def _store_stats_snapshot(self) -> dict[str, int]:
        stats = getattr(self._store, "stats", None)
        if stats is None:
            return {}
        return {
            "persistent_hits": int(getattr(stats, "persistent_hits", 0)),
            "persistent_misses": int(getattr(stats, "persistent_misses", 0)),
            "redis_warm_loads": int(getattr(stats, "redis_warm_loads", 0)),
        }

    def _store_stats_delta(self, before: dict[str, int]) -> dict[str, int]:
        after = self._store_stats_snapshot()
        return {
            key: max(0, after.get(key, 0) - before.get(key, 0))
            for key in ("persistent_hits", "persistent_misses", "redis_warm_loads")
        }
