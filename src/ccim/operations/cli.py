"""Task-oriented operational-readiness CLI."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ccim.operations.budget import evaluate_preflight
from ccim.operations.contracts import (
    SCHEMA_VERSION,
    ProjectMode,
    ReportLabel,
    RunCategory,
)
from ccim.operations.dry_run import DeterministicMockProvider, build_dry_run, build_runs
from ccim.operations.reporting import build_report
from ccim.operations.safety import assert_artifact_safe


def _envelope(
    *,
    ok: bool,
    command: str,
    data: dict[str, Any],
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": ok,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "data": data,
        "warnings": [],
        "errors": errors or [],
    }
    assert_artifact_safe(result)
    return result


def _emit(result: dict[str, Any], *, as_json: bool, output: Path | None) -> None:
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        if output.suffix.casefold() != ".json":
            raise ValueError("output must use the .json suffix")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    if as_json:
        print(encoded, end="")
    elif output is not None:
        print(f"operational_readiness=ok output={output.name}")
    else:
        print(
            f"operational_readiness={'ok' if result['ok'] else 'blocked'} "
            f"command={result['command']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CCIM operational-data readiness checks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser(
        "dry-run", help="run the deterministic mock-provider readiness fixture"
    )
    dry_run.add_argument("--json", action="store_true")
    dry_run.add_argument("--output", type=Path)

    report = subparsers.add_parser(
        "report", help="render a category-separated dummy report"
    )
    report.add_argument("--window-days", type=int, choices=(7, 30), required=True)
    report.add_argument("--json", action="store_true")
    report.add_argument("--output", type=Path)

    budget = subparsers.add_parser(
        "budget-check", help="evaluate one shared-canary request envelope"
    )
    budget.add_argument("--run-category", choices=[item.value for item in RunCategory], required=True)
    budget.add_argument("--project-mode", choices=[item.value for item in ProjectMode], required=True)
    budget.add_argument("--known-daily-tokens", type=int, required=True)
    budget.add_argument("--current-run-tokens", type=int, required=True)
    budget.add_argument("--expected-input-tokens", type=int, required=True)
    budget.add_argument("--max-output-tokens", type=int, required=True)
    budget.add_argument("--usage-certain", action="store_true")
    budget.add_argument("--json", action="store_true")
    budget.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command_name = f"ccim.operations {args.command}"
    try:
        if args.command == "dry-run":
            result = _envelope(ok=True, command=command_name, data=build_dry_run())
        elif args.command == "report":
            report = build_report(
                build_runs(),
                DeterministicMockProvider().build_observations(),
                window_days=args.window_days,
                report_label=ReportLabel.DUMMY,
            )
            result = _envelope(ok=True, command=command_name, data=report)
        else:
            decision = evaluate_preflight(
                run_category=RunCategory(args.run_category),
                project_mode=ProjectMode(args.project_mode),
                known_daily_tokens=args.known_daily_tokens,
                current_run_tokens=args.current_run_tokens,
                expected_input_tokens=args.expected_input_tokens,
                max_output_tokens=args.max_output_tokens,
                usage_certain=args.usage_certain,
            )
            result = _envelope(
                ok=decision.allowed,
                command=command_name,
                data={"decision": decision.as_dict()},
                errors=(
                    []
                    if decision.allowed
                    else [
                        {
                            "code": decision.reason_code,
                            "message": "provider call blocked by deterministic preflight",
                        }
                    ]
                ),
            )
        _emit(result, as_json=args.json, output=args.output)
        return 0 if result["ok"] else 1
    except Exception as exc:
        failure = {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "command": command_name,
            "data": {},
            "warnings": [],
            "errors": [
                {
                    "code": type(exc).__name__,
                    "message": "operational readiness command failed",
                }
            ],
        }
        if getattr(args, "json", False):
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f"operational_readiness=error code={type(exc).__name__} "
                f"command={command_name}"
            )
        return 1
