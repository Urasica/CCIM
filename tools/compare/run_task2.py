"""Create deterministic outputs for task2 compression-rate checks."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "tools" / "compare" / "workspace" / "task2"
REFERENCE = ROOT / "tools" / "compare" / "large_reference.py"


def main() -> None:
    if not REFERENCE.exists():
        raise FileNotFoundError(f"missing reference: {REFERENCE}")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    output = WORKSPACE / "output.md"
    lines = REFERENCE.read_text(encoding="utf-8").count("\n") + 1
    output.write_text(
        "\n".join(
            [
                "## Task2 - large_reference.py 읽기 완료",
                "",
                "- reference: tools/compare/large_reference.py",
                f"- lines: {lines}",
                "- purpose: compression-rate check",
                "",
                "## 분석 요청",
                "",
                "large_reference.py의 반복 구조와 압축 대상이 되는 함수 본문 패턴을 확인했습니다.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"WROTE {output}")


if __name__ == "__main__":
    main()
