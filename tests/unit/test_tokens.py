"""utils/tokens unit tests."""

from __future__ import annotations

from ccim.api.schemas import Message, MessagesRequest, TextBlock, ToolDefinition, ToolUseBlock
from ccim.utils.tokens import (
    estimate_message_tokens,
    estimate_request_tokens,
    estimate_text_tokens,
)


def test_estimate_text_tokens_zero_for_empty() -> None:
    assert estimate_text_tokens("") == 0


def test_estimate_text_tokens_positive_for_nonempty() -> None:
    assert estimate_text_tokens("hello world") > 0


def test_estimate_text_tokens_more_for_longer() -> None:
    short = estimate_text_tokens("hi")
    long_text = estimate_text_tokens("hi " * 1000)
    assert long_text > short


def test_estimate_message_tokens_string_content() -> None:
    m = Message(role="user", content="hello world")
    assert estimate_message_tokens(m) > estimate_text_tokens("hello world")


def test_estimate_message_tokens_block_content() -> None:
    m = Message(role="user", content=[TextBlock(text="hello world")])
    assert estimate_message_tokens(m) > 0


def test_estimate_message_tokens_tool_use_includes_input() -> None:
    m = Message(
        role="assistant",
        content=[
            ToolUseBlock(
                id="t1",
                name="retrieve_original",
                input={"context_id": "abc:001"},
            )
        ],
    )
    assert estimate_message_tokens(m) > 8


def test_estimate_request_tokens_includes_system_and_tools() -> None:
    req = MessagesRequest(
        model="claude-sonnet-4-6",
        system="system instructions " * 50,
        messages=[Message(role="user", content="hello world")],
        tools=[
            ToolDefinition(
                name="search",
                description="search project docs",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ],
    )
    message_only = sum(estimate_message_tokens(m) for m in req.messages)
    assert estimate_request_tokens(req) > message_only


def test_estimate_request_tokens_matches_messages_when_no_system_or_tools() -> None:
    req = MessagesRequest(
        model="claude-sonnet-4-6",
        messages=[Message(role="user", content="hello world")],
    )
    message_only = sum(estimate_message_tokens(m) for m in req.messages)
    assert estimate_request_tokens(req) == message_only
