"""Reject CI artifacts containing common secrets or developer absolute paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".sqlite", ".db"}
FORBIDDEN_TEXT = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    "credential_assignment": re.compile(
        r"(?i)(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY)\s*[:=]\s*[^\s\"']+"
    ),
    "windows_user_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    "runner_path": re.compile(r"/home/runner/work/[^\s\"']+"),
}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_artifact_safety.py <path> [<path> ...]")
        return 2
    violations: list[str] = []
    for raw in argv:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in files:
            if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                violations.append(f"{path}: forbidden artifact name")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in FORBIDDEN_TEXT.items():
                if pattern.search(text):
                    violations.append(f"{path}: {label}")
    if violations:
        print("unsafe CI artifact:")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("artifact_safety=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
