from __future__ import annotations

from ccim.compress.structured_outputs import (
    build_tool_result_reference,
    is_structured_output_candidate,
    should_dedupe_tool_result,
    summarize_command_output,
)


def _long_success_output() -> str:
    noise = "\n".join(f"test_case_{i} ... ok" for i in range(200))
    return f"{noise}\n\nRan 7 tests in 0.123s\n\nOK\n"


def test_success_unittest_output_is_summarized() -> None:
    raw = _long_success_output()
    summary = summarize_command_output(raw)
    assert summary is not None
    assert len(summary) < len(raw)
    assert "Ran 7 tests" in summary
    assert "OK" in summary


def test_short_output_is_not_candidate() -> None:
    assert not is_structured_output_candidate("Ran 1 test in 0.01s\n\nOK\n")


def test_short_tool_result_is_not_dedupe_candidate() -> None:
    assert not should_dedupe_tool_result("ok")


def test_failure_output_keeps_diagnostic_lines() -> None:
    raw = "\n".join(
        ["setup line"] * 200
        + [
            "Traceback (most recent call last):",
            '  File "test_example.py", line 10, in test_x',
            "AssertionError: expected 1",
            "Exit code 1",
        ]
    )
    summary = summarize_command_output(raw, is_error=True)
    assert summary is not None
    assert len(summary) < len(raw)
    assert "Traceback" in summary
    assert "AssertionError" in summary


def test_repeated_reference_is_compact() -> None:
    ref = build_tool_result_reference("a" * 64, chars=5000, lines=200)
    assert "Repeated tool_result omitted" in ref
    assert "sha256:" in ref
