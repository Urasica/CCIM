"""Ingress, response, and write-tool compatibility contracts."""

from ccim.compatibility.openai import (
    CompatibilityError,
    messages_to_openai_response,
    messages_to_openai_sse,
    openai_chat_to_messages,
)
from ccim.compatibility.write_tools import (
    WriteToolInspection,
    inspect_write_tool,
    write_tool_names,
)

__all__ = [
    "CompatibilityError",
    "WriteToolInspection",
    "inspect_write_tool",
    "messages_to_openai_response",
    "messages_to_openai_sse",
    "openai_chat_to_messages",
    "write_tool_names",
]
