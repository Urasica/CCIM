"""AST Compressor + Trigger 단위 테스트."""

from __future__ import annotations

import importlib.util as _ilu

import pytest

from ccim.api.schemas import Message, ToolResultBlock
from ccim.compress.ast_compressor import ASTCompressor
from ccim.compress.trigger import (
    detect_language_from_code,
    detect_language_from_fence,
    has_compressible_code,
    has_compressible_content,
    is_current_turn,
    select_compression_candidates,
    should_compress,
)

# ── ASTCompressor ─────────────────────────────────────────────────────


def test_function_body_masked_signature_preserved() -> None:
    code = (
        "import os\n"
        "\n"
        "def hello(name: str) -> str:\n"
        '    greeting = f"Hello, {name}"\n'
        "    return greeting\n"
        "\n"
        "x = 1\n"
    )
    cmp = ASTCompressor()
    result = cmp.compress(code, session_id="test")

    assert "import os" in result.compressed_text
    assert "def hello(name: str) -> str:" in result.compressed_text
    assert "x = 1" in result.compressed_text
    assert "<<CTX_test:001>>" in result.compressed_text
    # 본문 식별자 누출 없어야
    assert "greeting" not in result.compressed_text

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.context_id == "001"
    assert block.marker == "<<CTX_test:001>>"
    assert "greeting" in block.original_code
    assert block.original_lines == (4, 5)
    assert block.symbol_name == "hello"


def test_imports_preserved() -> None:
    code = (
        "import os\n"
        "from typing import Any\n"
        "\n"
        "def f():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
    )
    result = ASTCompressor().compress(code, session_id="t")
    assert "import os" in result.compressed_text
    assert "from typing import Any" in result.compressed_text


def test_line_mapping_monotonic() -> None:
    code = (
        "x = 1\n"
        "def f():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    return a + b\n"
        "y = 2\n"
    )
    result = ASTCompressor().compress(code, session_id="t")
    keys = sorted(result.line_mapping)
    values = [result.line_mapping[k] for k in keys]
    assert values == sorted(values), f"line_mapping not monotonic: {result.line_mapping}"
    # 마커 라인은 본문 첫 줄을 가리켜야
    block = result.blocks[0]
    assert result.line_mapping[block.marker_line] == block.original_lines[0]


def test_class_methods_compressed() -> None:
    code = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        x = 1\n"
        "        return x\n"
    )
    result = ASTCompressor().compress(code, session_id="t")
    assert "class Foo:" in result.compressed_text
    assert "def bar(self):" in result.compressed_text
    assert "<<CTX_t:001>>" in result.compressed_text
    # 메서드 본문은 마커 들여쓰기 8칸 유지
    assert "        <<CTX_t:001>>" in result.compressed_text


def test_short_body_skipped() -> None:
    """1줄짜리 본문은 절약 효과가 없어 마스킹하지 않는다."""
    code = "def small():\n    return 1\n"
    result = ASTCompressor().compress(code, session_id="t")
    assert "<<CTX" not in result.compressed_text
    assert result.blocks == []


def test_partial_parse_does_not_crash() -> None:
    """미완성 코드도 incremental parse가 부분 결과를 돌려주므로 크래시 금지."""
    code = "def broken(\n    x = 1\n"
    result = ASTCompressor().compress(code, session_id="t")
    assert isinstance(result.compressed_text, str)


def test_nested_function_only_outer_masked() -> None:
    code = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    result = ASTCompressor().compress(code, session_id="t")
    # 외부 함수 본문 1개만 마스킹되고, 내부 함수는 그 안에 포함되어 보존된 본문에서 다시 보임
    assert len(result.blocks) == 1
    assert "inner" in result.blocks[0].original_code
    # 내부 함수가 별도 ctx로 마스킹되지 않았음
    assert "<<CTX_t:002>>" not in result.compressed_text


def test_multiple_functions_get_distinct_ctx_ids() -> None:
    code = (
        "def a():\n"
        "    x = 1\n"
        "    return x\n"
        "def b():\n"
        "    y = 2\n"
        "    return y\n"
    )
    result = ASTCompressor().compress(code, session_id="s")
    assert {b.context_id for b in result.blocks} == {"001", "002"}
    assert "<<CTX_s:001>>" in result.compressed_text
    assert "<<CTX_s:002>>" in result.compressed_text


def test_repeated_functions_can_be_clustered() -> None:
    code = "\n\n".join(
        (
            f"def transform_batch_{i:03d}(records):\n"
            "    output = []\n"
            "    for raw in records:\n"
            f"        output.append((raw, {i}))\n"
            "    return output\n"
        )
        for i in range(1, 5)
    )
    normal = ASTCompressor().compress(code, session_id="t")
    clustered = ASTCompressor().compress(
        code,
        session_id="t",
        cluster_repeated_functions=True,
    )

    assert len(normal.blocks) == 4
    assert len(clustered.blocks) == 1
    assert "CCIM repeated function cluster" in clustered.compressed_text
    assert "transform_batch_001..transform_batch_004" in clustered.compressed_text
    assert "cluster_symbols=transform_batch_001" in clustered.compressed_text
    assert clustered.blocks[0].symbol_name == "transform_batch_001..transform_batch_004"
    assert clustered.blocks[0].symbol_index == (
        "transform_batch_001",
        "transform_batch_002",
        "transform_batch_003",
        "transform_batch_004",
    )
    assert "output.append" in clustered.blocks[0].original_code
    assert clustered.bytes_saved > normal.bytes_saved


def test_python_fact_manifest_preserves_calls_and_literal_writes() -> None:
    code = (
        "from typing import Any, Iterable, Mapping\n"
        "\n"
        "def transform_batch_040(records: Iterable[Mapping[str, Any]]) -> list[LargeRecord]:\n"
        "    payload = {}\n"
        "    payload['batch'] = 40\n"
        "    payload['offset'] = 1\n"
        "    return [LargeRecord(payload=payload)]\n"
        "\n"
        "def run_all(records):\n"
        "    for item in transform_batch_001(records):\n"
        "        status = item.payload['status']\n"
        "    return {'status': status}\n"
    )
    result = ASTCompressor().compress(code, session_id="t")

    assert (
        "# CCIM fact: transform_batch_040: signature=def transform_batch_040"
        in result.compressed_text
    )
    assert "payload['batch']=40" in result.compressed_text
    assert "# CCIM fact: run_all: calls=transform_batch_001" in result.compressed_text
    assert "item.payload['status']" in result.compressed_text
    assert result.blocks[0].fact_manifest


def test_python_fact_manifest_preserves_negative_relationships() -> None:
    transforms = "\n\n".join(
        (
            f"def transform_batch_{i:03d}(records):\n"
            "    payload = {}\n"
            f"    payload['batch'] = {i}\n"
            "    return payload\n"
        )
        for i in (1, 2, 3, 40)
    )
    code = (
        f"{transforms}\n\n"
        "def run_all(records):\n"
        "    result = transform_batch_001(records)\n"
        "    return result\n"
    )
    result = ASTCompressor().compress(
        code,
        session_id="t",
        cluster_repeated_functions=True,
    )

    assert (
        "run_all: relationship=calls transform_batch_001 "
        "from transform_batch_001..transform_batch_040"
    ) in result.compressed_text
    assert "run_all: does_not_call=transform_batch_040" in result.compressed_text


def test_tokens_after_less_than_before() -> None:
    code = (
        "def heavy():\n"
        + "".join(f"    var_{i} = {i}\n" for i in range(50))
        + "    return None\n"
    )
    result = ASTCompressor().compress(code, session_id="t")
    assert result.tokens_saved > 0
    assert result.bytes_saved > 0


# ── Trigger ───────────────────────────────────────────────────────────


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=text)


def test_trigger_below_threshold_returns_empty() -> None:
    msgs = [_msg("user", "hi")]
    assert should_compress(msgs, threshold_tokens=10_000, target_tokens=5_000) == []


def test_trigger_above_threshold_picks_compressible() -> None:
    code_block = (
        "```python\n" + "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n```\n"
    )
    big = "explanation " + code_block * 50
    msgs = [
        _msg("user", "previous question " + big),
        _msg("assistant", "previous answer " + big),
        _msg("user", "current question, no code"),
    ]
    selected = should_compress(msgs, threshold_tokens=500, target_tokens=200)
    assert selected, "압축 후보가 비어 있으면 안 됨"
    # 현재 턴(가장 최근 user)는 절대 포함 금지
    assert msgs[-1] not in selected


def test_trigger_uses_request_token_override_for_threshold() -> None:
    code_block = (
        "```python\n" + "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n```\n"
    )
    msgs = [
        _msg("user", "previous question " + code_block),
        _msg("assistant", "previous answer " + code_block),
        _msg("user", "current question"),
    ]
    selected = should_compress(
        msgs,
        threshold_tokens=500,
        target_tokens=200,
        request_tokens=700,
    )
    assert selected


def test_trigger_diagnostics_current_turn_excluded() -> None:
    code_block = (
        "```python\n" + "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n```\n"
    )
    msgs = [
        _msg("user", "old"),
        _msg("user", "current " + code_block),
    ]
    selected, diagnostics = select_compression_candidates(
        msgs,
        threshold_tokens=500,
        target_tokens=200,
        request_tokens=700,
    )
    assert selected == []
    assert diagnostics.skip_reason == "current_turn_excluded"
    assert diagnostics.current_turn_excluded == 1


def test_trigger_diagnostics_current_turn_excluded_with_no_prior_eligible() -> None:
    code_block = (
        "```python\n" + "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n```\n"
    )
    msgs = [
        _msg("user", "current " + code_block),
    ]
    selected, diagnostics = select_compression_candidates(
        msgs,
        threshold_tokens=500,
        target_tokens=200,
        request_tokens=700,
    )
    assert selected == []
    assert diagnostics.skip_reason == "current_turn_excluded"
    assert diagnostics.eligible_messages == 0
    assert diagnostics.current_turn_excluded == 1


def test_trigger_diagnostics_no_compressible_content() -> None:
    msgs = [
        _msg("user", "old plain text"),
        _msg("assistant", "old answer"),
        _msg("user", "current"),
    ]
    selected, diagnostics = select_compression_candidates(
        msgs,
        threshold_tokens=500,
        target_tokens=200,
        request_tokens=700,
    )
    assert selected == []
    assert diagnostics.skip_reason == "no_compressible_content"
    assert diagnostics.eligible_messages == 2


def test_trigger_picks_oldest_first() -> None:
    code_block = (
        "```python\n" + "\n".join(f"    line_{i} = {i}" for i in range(20)) + "\n```\n"
    )
    msgs = [
        _msg("user", "OLD " + code_block * 30),
        _msg("assistant", "MID " + code_block * 30),
        _msg("user", "current"),
    ]
    # threshold 위, target은 적당히 낮게 → 가장 오래된 후보부터 선택됨
    selected = should_compress(msgs, threshold_tokens=500, target_tokens=200)
    assert selected[0] is msgs[0]


def test_has_compressible_code_detects_python_fence() -> None:
    text = (
        "Here is some code:\n```python\n"
        + "\n".join(f"x{i} = 0" for i in range(10))
        + "\n```\n"
    )
    assert has_compressible_code(_msg("user", text))


def test_has_compressible_code_short_block_rejected() -> None:
    assert not has_compressible_code(_msg("user", "```python\nx=1\n```"))


def test_has_compressible_content_detects_structured_tool_output() -> None:
    noise = "\n".join(f"test_case_{i} ... ok" for i in range(200))
    output = f"{noise}\n\nRan 7 tests in 0.123s\n\nOK\n"
    msg = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="tool-1", content=output)],
    )
    assert has_compressible_content(msg)


def test_is_current_turn_last_user_message() -> None:
    msgs = [
        _msg("user", "old"),
        _msg("assistant", "answer"),
        _msg("user", "current"),
    ]
    assert not is_current_turn(msgs[0], msgs)
    assert not is_current_turn(msgs[1], msgs)
    assert is_current_turn(msgs[2], msgs)


# ── 언어 감지 ─────────────────────────────────────────────────────────


def test_detect_language_from_fence_aliases() -> None:
    assert detect_language_from_fence("java") == "java"
    assert detect_language_from_fence("JAVA") == "java"
    assert detect_language_from_fence("csharp") == "csharp"
    assert detect_language_from_fence("c#") == "csharp"
    assert detect_language_from_fence("cs") == "csharp"
    assert detect_language_from_fence("python") == "python"
    assert detect_language_from_fence("py") == "python"
    assert detect_language_from_fence(None) == "python"
    assert detect_language_from_fence("") == "python"


def test_detect_language_from_code_java_package() -> None:
    code = "package com.example.app;\npublic class Foo {\n    public void bar() {}\n}"
    assert detect_language_from_code(code) == "java"


def test_detect_language_from_code_java_annotation() -> None:
    code = "@Override\npublic String toString() {\n    return \"foo\";\n}"
    assert detect_language_from_code(code) == "java"


def test_detect_language_from_code_csharp_namespace() -> None:
    code = "namespace MyApp.Services;\npublic class Foo {\n    public void Bar() {}\n}"
    assert detect_language_from_code(code) == "csharp"


def test_detect_language_from_code_csharp_using() -> None:
    code = "using System;\nusing System.Collections.Generic;\npublic class Foo {}"
    assert detect_language_from_code(code) == "csharp"


def test_detect_language_from_code_python_default() -> None:
    code = "def foo():\n    return 1\n\nclass Bar:\n    pass\n"
    assert detect_language_from_code(code) == "python"


def test_has_compressible_code_detects_java_fence() -> None:
    text = (
        "Here is Java code:\n```java\n"
        + "\n".join(f"int x{i} = {i};" for i in range(10))
        + "\n```\n"
    )
    assert has_compressible_code(_msg("user", text))


def test_has_compressible_code_detects_csharp_fence() -> None:
    text = (
        "Here is C# code:\n```csharp\n"
        + "\n".join(f"int x{i} = {i};" for i in range(10))
        + "\n```\n"
    )
    assert has_compressible_code(_msg("user", text))


# ── Java/C# ASTCompressor (tree-sitter-java/c-sharp 설치 필요) ────────


@pytest.mark.skipif(
    _ilu.find_spec("tree_sitter_java") is None,
    reason="tree-sitter-java 미설치 (uv add tree-sitter-java)",
)
def test_java_method_body_compressed() -> None:
    code = (
        "public class Greeter {\n"
        "    public String greet(String name) {\n"
        "        String msg = helper(name);\n"
        "        System.out.println(msg);\n"
        "        return msg;\n"
        "    }\n"
        "    private String helper(String value) {\n"
        "        return value.trim();\n"
        "    }\n"
        "}\n"
    )
    result = ASTCompressor().compress(code, session_id="t", language="java")
    assert "<<CTX_t:001>>" in result.compressed_text
    assert "public String greet(String name)" in result.compressed_text
    assert "String msg = helper(name);" not in result.compressed_text
    assert "# CCIM fact: greet: calls=helper, System.out.println" in result.compressed_text
    assert "# CCIM fact: greet: writes=msg=helper(name)" in result.compressed_text
    assert result.blocks[0].fact_manifest
    assert result.blocks[0].symbol_index == ("greet",)
    assert len(result.blocks) == 2


@pytest.mark.skipif(
    _ilu.find_spec("tree_sitter_c_sharp") is None,
    reason="tree-sitter-c-sharp 미설치 (uv add tree-sitter-c-sharp)",
)
def test_csharp_method_body_compressed() -> None:
    code = (
        "public class Greeter {\n"
        "    public string Greet(string name) {\n"
        "        var msg = Helper(name);\n"
        "        Console.WriteLine(msg);\n"
        "        return msg;\n"
        "    }\n"
        "    private string Helper(string value) {\n"
        "        return value.Trim();\n"
        "    }\n"
        "}\n"
    )
    result = ASTCompressor().compress(code, session_id="t", language="csharp")
    assert "<<CTX_t:001>>" in result.compressed_text
    assert "public string Greet(string name)" in result.compressed_text
    assert "var msg = Helper(name);" not in result.compressed_text
    assert "# CCIM fact: Greet: calls=Helper, Console.WriteLine" in result.compressed_text
    assert "# CCIM fact: Greet: writes=msg=Helper(name)" in result.compressed_text
    assert result.blocks[0].fact_manifest
    assert result.blocks[0].symbol_index == ("Greet",)
    assert len(result.blocks) == 2
