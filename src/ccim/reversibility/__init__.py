"""Reversibility Layer - Redis-backed original storage + retrieve_original tool.

Design section 3.2.3:
  - Inject the retrieve_original tool definition into the upstream system prompt.
  - Intercept the LLM's tool_use, resolve from Redis, send tool_result back.
"""

from ccim.reversibility.interceptor import (
    RETRIEVE_TOOL_NAME,
    InterceptStats,
    ReversibilityInterceptor,
    ToolResolution,
)
from ccim.reversibility.retrieve_tool import RETRIEVE_ORIGINAL_TOOL, build_system_hint
from ccim.reversibility.store import ContextRecord, ReversibilityStore

__all__ = [
    "RETRIEVE_ORIGINAL_TOOL",
    "RETRIEVE_TOOL_NAME",
    "ContextRecord",
    "InterceptStats",
    "ReversibilityInterceptor",
    "ReversibilityStore",
    "ToolResolution",
    "build_system_hint",
]
