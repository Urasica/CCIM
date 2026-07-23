"""Artifact safety checks for operational summaries."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "original_code",
    "prompt",
    "raw_prompt",
    "secret",
    "source_path",
    "source_text",
}
_FORBIDDEN_TEXT = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    "credential_assignment": re.compile(
        r"(?i)(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY)"
        r"\s*[:=]\s*[^\s\"']+"
    ),
    "windows_user_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    "unix_home_path": re.compile(r"/(?:home|Users)/[^/\s]+/"),
}


def scan_text(value: str) -> tuple[str, ...]:
    return tuple(label for label, pattern in _FORBIDDEN_TEXT.items() if pattern.search(value))


def artifact_violations(value: Any) -> tuple[str, ...]:
    violations: list[str] = []

    def visit(item: Any, location: str) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                child_location = f"{location}.{key}"
                if key.casefold() in _FORBIDDEN_KEYS:
                    violations.append(f"{child_location}:forbidden_key")
                visit(child, child_location)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")
            return
        if isinstance(item, str):
            violations.extend(f"{location}:{label}" for label in scan_text(item))

    visit(value, "$")
    return tuple(sorted(set(violations)))


def assert_artifact_safe(value: Any) -> None:
    violations = artifact_violations(value)
    if violations:
        raise ValueError(f"unsafe operational artifact: {violations[0]}")


def parse_and_check_json(text: str) -> tuple[str, ...]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return scan_text(text)
    return artifact_violations(value)
