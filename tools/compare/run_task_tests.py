"""Run compare task tests and write a clean test_result.txt."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "tools" / "compare" / "workspace" / "current"
TEST_FILE = WORKSPACE / "test_reference_pipeline.py"
RESULT_FILE = WORKSPACE / "test_result.txt"


def main() -> int:
    if not TEST_FILE.exists():
        raise FileNotFoundError(f"missing test file: {TEST_FILE}")

    proc = subprocess.run(
        [sys.executable, str(TEST_FILE), "-v"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    RESULT_FILE.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    print(f"TEST_EXIT={proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
