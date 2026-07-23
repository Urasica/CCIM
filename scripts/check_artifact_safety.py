"""Reject CI artifacts containing common secrets or developer absolute paths."""

from __future__ import annotations

import sys
from pathlib import Path

from ccim.operations.safety import parse_and_check_json

FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".sqlite", ".db"}


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
            for violation in parse_and_check_json(text):
                violations.append(f"{path}: {violation}")
    if violations:
        print("unsafe CI artifact:")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("artifact_safety=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
