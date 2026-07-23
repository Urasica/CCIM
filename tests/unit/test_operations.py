from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ccim.operations.budget import evaluate_preflight
from ccim.operations.cli import main
from ccim.operations.contracts import (
    ProjectMode,
    ReportLabel,
    RetentionPolicy,
    RunCategory,
    RunMetadata,
    hash_project,
)
from ccim.operations.dry_run import (
    DeterministicMockProvider,
    build_dry_run,
    build_runs,
)
from ccim.operations.reporting import build_report
from ccim.operations.safety import artifact_violations, parse_and_check_json


def test_project_hash_is_stable_and_does_not_expose_identifier() -> None:
    first = hash_project("private-project", salt="stable-salt")
    second = hash_project("private-project", salt="stable-salt")

    assert first == second
    assert len(first) == 64
    assert "private-project" not in first


def test_retention_contract_separates_evidence_telemetry_and_artifacts() -> None:
    policy = RetentionPolicy()

    assert policy.evidence_ttl_seconds == 3_600
    assert policy.telemetry_days == 90
    assert policy.artifact_days == 14
    assert policy.persistent_evidence_backup_mode == "private-encrypted-only"

    with pytest.raises(ValueError, match="telemetry_expires_at is required"):
        replace(build_runs()[0], telemetry_expires_at=None)


def test_run_contract_rejects_category_project_mismatch() -> None:
    run = build_runs()[0]
    values = {
        field: getattr(run, field)
        for field in RunMetadata.__dataclass_fields__
    }
    values["project_mode"] = ProjectMode.PRIVATE_PRODUCTION
    values["project_hash"] = "a" * 64

    with pytest.raises(ValueError, match="requires project_mode=none"):
        RunMetadata(**values)


@pytest.mark.parametrize(
    ("overrides", "expected_allowed", "expected_reason"),
    [
        ({}, True, "allowed"),
        ({"known_daily_tokens": 1_900_001}, False, "daily_hard_stop"),
        ({"current_run_tokens": 700_001}, False, "run_hard_cap"),
        ({"expected_input_tokens": 180_001}, False, "request_input_cap"),
        ({"max_output_tokens": 20_001}, False, "request_output_cap"),
        ({"usage_certain": False}, False, "usage_uncertain"),
        (
            {
                "run_category": RunCategory.SYNTHETIC_DRY_RUN,
                "project_mode": ProjectMode.NONE,
            },
            False,
            "dry_run_external_call_forbidden",
        ),
    ],
)
def test_budget_preflight_boundaries(
    overrides: dict[str, object],
    expected_allowed: bool,
    expected_reason: str,
) -> None:
    values = {
        "run_category": RunCategory.DAILY_CANARY,
        "project_mode": ProjectMode.SHARED_SYNTHETIC,
        "known_daily_tokens": 1_900_000,
        "current_run_tokens": 700_000,
        "expected_input_tokens": 180_000,
        "max_output_tokens": 20_000,
        "usage_certain": True,
    }
    values.update(overrides)

    decision = evaluate_preflight(**values)  # type: ignore[arg-type]

    assert decision.allowed is expected_allowed
    assert decision.reason_code == expected_reason
    assert decision.safety_reserve == 400_000


def test_report_keeps_categories_separate_and_calculates_net_saving() -> None:
    runs = build_runs()
    observations = DeterministicMockProvider().build_observations()

    report = build_report(
        runs,
        observations,
        window_days=7,
        report_label=ReportLabel.DRY_RUN,
    )

    categories = {
        item["run_category"]: item for item in report["categories"]
    }
    assert set(categories) == {
        "synthetic-dry-run",
        "daily-canary",
        "personal-production",
    }
    synthetic_runs = {
        item["run"]["run_id"]: item
        for item in categories["synthetic-dry-run"]["runs"]
    }
    assert synthetic_runs["dry-baseline"]["telemetry_completeness_pct"] == 80.0
    assert synthetic_runs["dry-compressed"]["gross_saved_tokens_est"] == 700
    assert synthetic_runs["dry-compressed"]["retrieve_overhead_tokens_est"] == 150
    assert synthetic_runs["dry-compressed"]["net_saved_tokens_est"] == 550
    assert report["actual_data"] is False


def test_report_rejects_duplicate_attempts() -> None:
    run = build_runs()[0]
    observation = DeterministicMockProvider().build_observations()[0]

    with pytest.raises(ValueError, match="duplicate request observation attempt"):
        build_report(
            [run],
            [observation, observation],
            window_days=7,
            report_label=ReportLabel.DRY_RUN,
        )


def test_dry_run_covers_all_states_without_external_calls() -> None:
    result = build_dry_run()

    assert result["provider"]["network_calls"] == 0
    assert result["provider"]["external_provider_calls"] == 0
    assert result["status_coverage"] == [
        "failed",
        "incomplete",
        "retry",
        "skipped",
        "succeeded",
    ]
    assert result["reports"]["weekly"]["report_label"] == "dry-run"
    assert result["reports"]["thirty_day"]["report_label"] == "dummy"
    assert result["actual_data"] is False


def test_artifact_safety_rejects_sensitive_keys_and_paths() -> None:
    unsafe = {
        "prompt": "do not export",
        "metadata": {"note": "C:\\Users\\developer\\private\\file.py"},
    }

    violations = artifact_violations(unsafe)

    assert "$.prompt:forbidden_key" in violations
    assert any("windows_user_path" in item for item in violations)

    with pytest.raises(ValueError, match="unsafe operational artifact"):
        replace(build_runs()[0], model="C:\\Users\\developer\\model")


def test_operations_cli_emits_stable_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["schema_version"] == "1"
    assert payload["command"] == "ccim.operations dry-run"
    assert parse_and_check_json(json.dumps(payload)) == ()


def test_budget_cli_returns_nonzero_for_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "budget-check",
            "--run-category",
            "daily-canary",
            "--project-mode",
            "shared-synthetic",
            "--known-daily-tokens",
            "2050000",
            "--current-run-tokens",
            "0",
            "--expected-input-tokens",
            "40000",
            "--max-output-tokens",
            "20000",
            "--usage-certain",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "daily_hard_stop"


def test_report_cli_writes_safe_dummy_artifact(tmp_path: Path) -> None:
    target = tmp_path / "report.json"

    exit_code = main(
        ["report", "--window-days", "30", "--output", str(target)]
    )

    assert exit_code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["data"]["report_label"] == "dummy"
    assert payload["data"]["actual_data"] is False
    assert parse_and_check_json(target.read_text(encoding="utf-8")) == ()
