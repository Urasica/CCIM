"""Create a compact artifact without test source, prompts, or host paths."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def junit_totals(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
        "seconds": round(
            sum(float(suite.attrib.get("time", 0.0)) for suite in suites), 3
        ),
    }


def coverage_totals(path: Path) -> dict[str, int | float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    totals: dict[str, Any] = payload.get("totals", {})
    return {
        "covered_lines": int(totals.get("covered_lines", 0)),
        "num_statements": int(totals.get("num_statements", 0)),
        "percent_covered": round(float(totals.get("percent_covered", 0.0)), 2),
        "missing_lines": int(totals.get("missing_lines", 0)),
    }


def main() -> int:
    args = build_parser().parse_args()
    summary: dict[str, Any] = {
        "schema": "ccim-ci-summary-v1",
        "suite": args.suite,
        "results": junit_totals(args.junit),
    }
    if args.coverage is not None:
        summary["coverage"] = coverage_totals(args.coverage)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
