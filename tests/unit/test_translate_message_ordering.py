"""translate._message_to_openai — tool 메시지 순서 회귀 테스트.

OpenAI 요구사항:
  - assistant(tool_calls) 바로 다음에 tool 메시지가 와야 한다.
  - user 메시지에 tool_result + text가 섞여 있을 때 tool_result 먼저 출력.

기존 test_llm_client.py가 다루는 항목:
  - tool_result → role=tool 변환 (단일, 순수 tool_result only)
  - tool_calls → assistant 메시지에 tool_calls 키 삽입
  - 단순 텍스트 메시지 변환

이 파일이 추가로 다루는 항목 (기존과 비중복):
  - user 메시지에 tool_result + text 혼합 시 순서 보장
  - 여러 tool_result + text 혼합
  - 전체 대화 시퀀스의 OpenAI 메시지 순서 제약 검증
  - anthropic_to_openai_request 레벨에서 end-to-end 순서 검증
"""

from __future__ import annotations

import json

from ccim.api.schemas import (
    Message,
    MessagesRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from ccim.llm.translate import _message_to_openai, anthropic_to_openai_request

# ─────────────────────────────────────────────────────────────────────
# _message_to_openai 단위 케이스
# ─────────────────────────────────────────────────────────────────────


def test_user_tool_result_before_text_mixed() -> None:
    """핵심 회귀 테스트: user 메시지에 tool_result + text 혼합 → tool 먼저."""
    msg = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="call_abc", content="파일 내용입니다"),
            TextBlock(text="이 결과를 바탕으로 설명해줘"),
        ],
    )
    out = _message_to_openai(msg)

    assert len(out) == 2
    # tool 메시지가 반드시 먼저 나와야 한다
    assert out[0]["role"] == "tool", f"첫 번째 메시지가 tool이어야 함: {out[0]['role']}"
    assert out[0]["tool_call_id"] == "call_abc"
    assert out[0]["content"] == "파일 내용입니다"
    # user 텍스트가 그 다음
    assert out[1]["role"] == "user"
    assert out[1]["content"] == "이 결과를 바탕으로 설명해줘"


def test_user_text_before_tool_result_in_input_still_tool_first() -> None:
    """입력 순서와 무관하게 tool_result가 먼저 출력되어야 한다."""
    msg = Message(
        role="user",
        content=[
            # 의도적으로 text를 먼저 나열
            TextBlock(text="분석해줘"),
            ToolResultBlock(tool_use_id="call_xyz", content="결과값"),
        ],
    )
    out = _message_to_openai(msg)

    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call_xyz"
    assert out[1]["role"] == "user"
    assert out[1]["content"] == "분석해줘"


def test_user_multiple_tool_results_before_text() -> None:
    """tool_result가 여러 개인 경우 모두 user text보다 먼저 출력."""
    msg = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="call_1", content="결과 A"),
            ToolResultBlock(tool_use_id="call_2", content="결과 B"),
            TextBlock(text="둘 다 합쳐서 설명해"),
        ],
    )
    out = _message_to_openai(msg)

    roles = [m["role"] for m in out]
    # tool 두 개가 먼저, user가 마지막
    assert roles == ["tool", "tool", "user"]
    assert out[0]["tool_call_id"] == "call_1"
    assert out[1]["tool_call_id"] == "call_2"
    assert out[2]["content"] == "둘 다 합쳐서 설명해"


def test_user_tool_result_only_no_extra_user_message() -> None:
    """tool_result만 있고 text가 없으면 user 메시지가 추가되면 안 된다."""
    msg = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="call_only", content="결과만"),
        ],
    )
    out = _message_to_openai(msg)

    assert len(out) == 1
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call_only"


def test_user_text_only_no_tool_messages() -> None:
    """일반 user 텍스트만 있는 경우 tool 메시지가 생기면 안 된다."""
    msg = Message(role="user", content=[TextBlock(text="안녕하세요")])
    out = _message_to_openai(msg)

    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "안녕하세요"


def test_assistant_with_tool_calls_stays_single_message() -> None:
    """assistant + tool_calls는 단일 메시지로 유지 (OpenAI 스펙)."""
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="파일을 읽겠습니다."),
            ToolUseBlock(id="call_read", name="read_file", input={"path": "/tmp/x.py"}),
        ],
    )
    out = _message_to_openai(msg)

    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" in out[0]
    assert out[0]["tool_calls"][0]["id"] == "call_read"
    assert out[0]["tool_calls"][0]["function"]["name"] == "read_file"
    # 텍스트도 포함되어야 함
    assert out[0]["content"] == "파일을 읽겠습니다."


def test_assistant_text_only_no_tool_calls_key() -> None:
    """assistant가 텍스트만 반환하면 tool_calls 키가 없어야 한다."""
    msg = Message(role="assistant", content=[TextBlock(text="완료했습니다.")])
    out = _message_to_openai(msg)

    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" not in out[0]


# ─────────────────────────────────────────────────────────────────────
# 전체 대화 시퀀스 순서 제약 검증
# ─────────────────────────────────────────────────────────────────────


def _validate_openai_message_sequence(messages: list[dict]) -> list[str]:
    """OpenAI 메시지 시퀀스 규칙 위반 사항을 반환 (빈 리스트 = 정상).

    규칙: 연속된 tool 메시지 블록의 첫 번째 앞에만 assistant(tool_calls)가 있으면 된다.
    (병렬 tool call 응답은 tool 메시지가 연속으로 나열된다.)
    """
    errors: list[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role != "tool":
            continue
        # 연속 tool 메시지 중 첫 번째인 경우에만 앞 메시지를 검사
        if i > 0 and messages[i - 1].get("role") == "tool":
            continue  # 앞도 tool → 연속 블록의 중간/끝, 검사 불필요
        prev = messages[i - 1] if i > 0 else None
        if prev is None or prev.get("role") != "assistant":
            errors.append(
                f"[{i}] tool 블록 첫 메시지 앞이 assistant가 아님: "
                f"{prev.get('role') if prev else 'None'}"
            )
        elif "tool_calls" not in prev:
            errors.append(
                f"[{i}] 앞 assistant 메시지에 tool_calls 없음"
            )
    return errors


def test_full_tool_call_sequence_valid() -> None:
    """도구 호출 포함 전체 대화의 OpenAI 메시지 순서 검증."""
    req = MessagesRequest(
        model="gpt-4o-mini",
        system="You are a helpful assistant.",
        messages=[
            # Turn 1: user 요청
            Message(role="user", content="test.py를 읽어줘"),
            # Turn 2: assistant가 tool 호출
            Message(
                role="assistant",
                content=[
                    TextBlock(text="파일을 읽겠습니다."),
                    ToolUseBlock(id="call_read_1", name="read_file", input={"path": "test.py"}),
                ],
            ),
            # Turn 3: tool 결과 + user 후속 질문 (혼합)
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_read_1", content="def hello(): pass"),
                    TextBlock(text="이 함수를 설명해줘"),
                ],
            ),
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    messages = body["messages"]

    # 순서 규칙 검증
    errors = _validate_openai_message_sequence(messages)
    assert errors == [], "시퀀스 오류:\n" + "\n".join(errors)

    # 내용 검증
    roles = [m["role"] for m in messages]
    # system, user, assistant(tool_calls), tool, user
    assert roles == ["system", "user", "assistant", "tool", "user"], (
        f"기대 순서: system/user/assistant/tool/user\n실제: {roles}"
    )


def test_multi_tool_call_sequence_valid() -> None:
    """여러 도구를 동시에 호출하는 시나리오 순서 검증."""
    req = MessagesRequest(
        model="gpt-4o-mini",
        messages=[
            Message(role="user", content="두 파일을 비교해줘"),
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="call_a", name="read_file", input={"path": "a.py"}),
                    ToolUseBlock(id="call_b", name="read_file", input={"path": "b.py"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_a", content="# a.py contents"),
                    ToolResultBlock(tool_use_id="call_b", content="# b.py contents"),
                ],
            ),
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    messages = body["messages"]

    errors = _validate_openai_message_sequence(messages)
    assert errors == [], "시퀀스 오류:\n" + "\n".join(errors)

    # tool 메시지 두 개 + 그 앞에 assistant 확인
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {"call_a", "call_b"}


def test_two_round_tool_exchange_sequence_valid() -> None:
    """두 번의 도구 호출 라운드가 연속될 때 시퀀스 검증."""
    req = MessagesRequest(
        model="gpt-4o-mini",
        messages=[
            Message(role="user", content="파일 읽고 수정해줘"),
            # 첫 번째 도구 호출
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="call_r1", name="read_file", input={"path": "f.py"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_r1", content="original content"),
                ],
            ),
            # 두 번째 도구 호출
            Message(
                role="assistant",
                content=[
                    TextBlock(text="수정하겠습니다."),
                    ToolUseBlock(id="call_w1", name="write_file",
                                 input={"path": "f.py", "content": "new content"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_w1", content="ok"),
                    TextBlock(text="수정 완료됐어?"),
                ],
            ),
        ],
    )
    body = anthropic_to_openai_request(req, stream=False)
    messages = body["messages"]

    errors = _validate_openai_message_sequence(messages)
    assert errors == [], "시퀀스 오류:\n" + "\n".join(errors)

    # 마지막이 user 텍스트인지 확인
    assert messages[-1]["role"] == "user"
    assert "수정 완료됐어?" in messages[-1]["content"]


def test_tool_result_list_content_serialized() -> None:
    """tool_result content가 list[dict]이면 JSON string으로 직렬화되어야 한다.

    ToolResultBlock.content 타입: str | list[dict[str, Any]]
    translate.py는 str이 아닌 경우 json.dumps로 직렬화한다.
    """
    msg = Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="call_j",
                content=[{"type": "text", "text": "lines: 42"}],
            ),
        ],
    )
    out = _message_to_openai(msg)
    assert out[0]["role"] == "tool"
    # list는 JSON string으로 변환되어야 함
    content = out[0]["content"]
    assert isinstance(content, str)
    parsed = json.loads(content)
    assert isinstance(parsed, list)
    assert parsed[0]["type"] == "text"
    assert "42" in parsed[0]["text"]
