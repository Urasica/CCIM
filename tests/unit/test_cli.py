"""ccim CLI contract tests."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ccim.cli import main


def test_run_dry_run_prints_safe_process_scoped_plan(capsys: object) -> None:
    parent_before = dict(os.environ)
    with patch("ccim.cli.subprocess.run") as run_mock:
        exit_code = main(
            [
                "run",
                "--dry-run",
                "--json",
                "--session",
                "roadmap-04",
                "--",
                "claude",
                "-p",
                "hello",
            ]
        )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["parent_environment_modified"] is False
    assert payload["environment"]["ANTHROPIC_AUTH_TOKEN"] == "<ccim-session-token>"
    assert os.environ == parent_before
    run_mock.assert_not_called()


def test_run_executes_claude_with_child_only_environment(capsys: object) -> None:
    captured: dict[str, object] = {}

    def _run(command: list[str], *, env: dict[str, str], check: bool) -> object:
        captured["command"] = command
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=7)

    with patch("ccim.cli.subprocess.run", side_effect=_run):
        exit_code = main(
            [
                "run",
                "--session",
                "session-123",
                "--endpoint",
                "http://127.0.0.1:9090/",
                "--",
                "claude",
            ]
        )

    assert exit_code == 7
    assert captured["command"] == ["claude"]
    child_env = captured["env"]
    assert child_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9090"
    assert child_env["ANTHROPIC_AUTH_TOKEN"] == "ccim-session-session-123"
    assert child_env["CCIM_LAUNCH_SESSION"] == "session-123"
    assert "ccim-session-session-123" not in capsys.readouterr().out


def test_doctor_json_uses_stable_exit_contract(capsys: object) -> None:
    report = {
        "schema_version": "1",
        "command": "ccim doctor",
        "ok": False,
        "checks": [
            {
                "name": "redis",
                "status": "fail",
                "required": True,
                "reason": "connection_failed",
            }
        ],
        "summary": {"pass": 0, "fail": 1, "skipped": 0},
    }
    with (
        patch(
            "ccim.diagnostics.collect_doctor_report",
            new=AsyncMock(return_value=report),
        ),
        patch("ccim.config.get_settings", return_value=object()),
    ):
        exit_code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["schema_version"] == "1"
    assert payload["checks"][0]["reason"] == "connection_failed"
