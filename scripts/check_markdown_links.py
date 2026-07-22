"""Fail when a repository Markdown file links to a missing local target."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SKIP_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "output", "tmp"}


def markdown_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if not SKIP_DIRS.intersection(path.relative_to(root).parts)
    )


def local_target(raw: str) -> str | None:
    value = raw.strip().strip("<>")
    if value.startswith(("http://", "https://", "mailto:", "data:", "#")):
        return None
    value = value.split("#", 1)[0].strip()
    return unquote(value) or None


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    missing: list[str] = []
    checked = 0
    for document in markdown_files(root):
        text = document.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = local_target(match.group(1))
            if target is None:
                continue
            checked += 1
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(root)} -> {target}")
    if missing:
        print("missing local Markdown links:")
        for item in missing:
            print(f"  - {item}")
        return 1
    print(f"markdown_links=ok checked={checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
