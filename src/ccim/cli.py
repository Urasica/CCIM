"""Stable command-line entry point for gateway, diagnostics, and host launch."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccim")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="read-only deployment checks")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--offline", action="store_true")
    doctor.add_argument("--timeout-s", type=float, default=2.0)

    launch = subparsers.add_parser("run", help="launch a supported coding-agent host")
    launch.add_argument("--host", choices=("claude-code",), default="claude-code")
    launch.add_argument("--endpoint", default="http://127.0.0.1:8080")
    launch.add_argument("--session", default=None)
    launch.add_argument("--dry-run", action="store_true")
    launch.add_argument("--json", action="store_true", dest="as_json")
    launch.add_argument("agent_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        from ccim.main import run as run_gateway

        run_gateway()
        return 0

    args = build_parser().parse_args(args_list)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "run":
        return _launch(args)
    build_parser().print_help()
    return 2


def _doctor(args: argparse.Namespace) -> int:
    if args.timeout_s <= 0:
        print("doctor_error=timeout_must_be_positive")
        return 2
    from ccim.config import get_settings
    from ccim.diagnostics import collect_doctor_report

    report = asyncio.run(
        collect_doctor_report(
            get_settings(),
            offline=bool(args.offline),
            timeout_s=float(args.timeout_s),
        )
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"doctor_ok={str(report['ok']).lower()} schema_version=1")
        for check in report["checks"]:
            print(
                "check="
                f"{check['name']} status={check['status']} "
                f"required={str(check['required']).lower()} reason={check['reason']}"
            )
    return 0 if report["ok"] else 1


def _launch(args: argparse.Namespace) -> int:
    command = list(args.agent_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("launch_error=agent_command_required")
        return 2
    executable = Path(command[0]).stem.lower()
    if args.host == "claude-code" and executable != "claude":
        print("launch_error=claude_code_command_must_start_with_claude")
        return 2

    endpoint = _validated_endpoint(args.endpoint)
    if endpoint is None:
        print("launch_error=invalid_endpoint")
        return 2
    session = args.session or f"ccim-{uuid.uuid4().hex[:12]}"
    if re.fullmatch(r"[A-Za-z0-9\-]+", session) is None:
        print("launch_error=invalid_session")
        return 2

    scoped_values = {
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": f"ccim-session-{session}",
        "ANTHROPIC_API_KEY": "ccim-local",
        "CCIM_LAUNCH_SESSION": session,
    }
    safe_environment = {
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": "<ccim-session-token>",
        "ANTHROPIC_API_KEY": "<local-gateway-token>",
        "CCIM_LAUNCH_SESSION": session,
    }
    plan: dict[str, Any] = {
        "schema_version": "1",
        "command": "ccim run",
        "host": args.host,
        "dry_run": bool(args.dry_run),
        "agent_command": command,
        "environment": safe_environment,
        "parent_environment_modified": False,
    }
    if args.as_json:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"launch_host={args.host} dry_run={str(bool(args.dry_run)).lower()} "
            f"session={session}"
        )
        print(f"ANTHROPIC_BASE_URL={endpoint}")
        print("ANTHROPIC_AUTH_TOKEN=<ccim-session-token>")
        print("ANTHROPIC_API_KEY=<local-gateway-token>")
        print(f"agent_command={json.dumps(command, ensure_ascii=False)}")
    if args.dry_run:
        return 0

    child_environment = os.environ.copy()
    child_environment.update(scoped_values)
    try:
        completed = subprocess.run(
            command,
            env=child_environment,
            check=False,
        )
    except FileNotFoundError:
        print("launch_error=agent_command_not_found")
        return 127
    return int(completed.returncode)


def _validated_endpoint(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return value.rstrip("/")


if __name__ == "__main__":
    raise SystemExit(main())
