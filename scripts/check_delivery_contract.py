"""Validate cloud-neutral single-VM delivery boundaries without cloud access."""

from __future__ import annotations

import re
from pathlib import Path

_DEPRECATED_NODE_ACTION = re.compile(
    r"actions/(?:checkout|upload-artifact)@v(?:1|2|3|4|5)(?:\s|$)"
)


def _require(text: str, expected: str, *, location: str, errors: list[str]) -> None:
    if expected not in text:
        errors.append(f"{location}: missing {expected!r}")


def check_repository(root: Path) -> list[str]:
    errors: list[str] = []
    ci_path = root / ".github" / "workflows" / "ci.yml"
    delivery_path = root / ".github" / "workflows" / "delivery.yml"
    compose_path = root / "docker-compose.yml"
    deploy_path = root / "deploy" / "single-vm" / "deploy.sh"
    canary_path = root / "deploy" / "single-vm" / "canary-dry-run.sh"
    backup_path = root / "deploy" / "single-vm" / "backup.sh"
    restore_path = root / "deploy" / "single-vm" / "restore.sh"

    paths = (
        ci_path,
        delivery_path,
        compose_path,
        deploy_path,
        canary_path,
        backup_path,
        restore_path,
    )
    missing = [path for path in paths if not path.is_file()]
    if missing:
        return [f"missing delivery file: {path.relative_to(root)}" for path in missing]

    ci = ci_path.read_text(encoding="utf-8")
    delivery = delivery_path.read_text(encoding="utf-8")
    compose = compose_path.read_text(encoding="utf-8")
    deploy = deploy_path.read_text(encoding="utf-8")
    canary = canary_path.read_text(encoding="utf-8")
    backup = backup_path.read_text(encoding="utf-8")
    restore = restore_path.read_text(encoding="utf-8")

    for path, workflow in ((ci_path, ci), (delivery_path, delivery)):
        match = _DEPRECATED_NODE_ACTION.search(workflow)
        if match is not None:
            errors.append(
                f"{path.relative_to(root)}: deprecated Node action {match.group(0).strip()}"
            )

    _require(ci, "actions/checkout@v6", location="ci.yml", errors=errors)
    _require(ci, "actions/upload-artifact@v6", location="ci.yml", errors=errors)
    if "self-hosted" in ci or "packages: write" in ci:
        errors.append("ci.yml: CI-only workflow must not access production delivery")

    delivery_requirements = (
        'workflows: ["CI"]',
        "branches: [master]",
        "vars.CCIM_DELIVERY_ENABLED == 'true'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'master'",
        "cancel-in-progress: false",
        "packages: write",
        "packages: read",
        "environment:",
        "name: production",
        "runs-on: [self-hosted, linux, x64, ccim-production]",
        "actions/checkout@v6",
        "actions/upload-artifact@v6",
        "deploy/single-vm/deploy.sh",
        "deploy/single-vm/canary-dry-run.sh",
    )
    for expected in delivery_requirements:
        _require(delivery, expected, location="delivery.yml", errors=errors)
    if "pull_request:" in delivery:
        errors.append("delivery.yml: production delivery must not have a pull_request trigger")

    if compose.count("image: ${CCIM_IMAGE_REF:-ccim:local}") != 2:
        errors.append("docker-compose.yml: gateway and migrate must share CCIM_IMAGE_REF")
    _require(
        compose,
        "path: ${CCIM_RUNTIME_ENV_FILE:-.env}",
        location="docker-compose.yml",
        errors=errors,
    )
    _require(compose, '"127.0.0.1:8080:8080"', location="docker-compose.yml", errors=errors)
    if re.search(r'^\s*-\s*["\']?(?:6379|5432):', compose, flags=re.MULTILINE):
        errors.append("docker-compose.yml: Redis/PostgreSQL must not publish host ports")

    deploy_requirements = (
        "@sha256:",
        "org.opencontainers.image.revision",
        "last-known-good.env",
        "flock -n",
        'rollback_status="succeeded"',
        "compose_migration_or_readiness",
    )
    for expected in deploy_requirements:
        _require(deploy, expected, location="deploy.sh", errors=errors)

    canary_requirements = (
        "python -m ccim.operations dry-run --json",
        "python -m ccim.migrations check --json",
        "external_provider_calls",
        "scripts/check_artifact_safety.py",
    )
    for expected in canary_requirements:
        _require(canary, expected, location="canary-dry-run.sh", errors=errors)

    backup_requirements = (
        "CCIM_BACKUP_AGE_RECIPIENT",
        "pg_dump",
        "redis-cli SAVE",
        "age --recipient",
    )
    for expected in backup_requirements:
        _require(backup, expected, location="backup.sh", errors=errors)

    restore_requirements = (
        "RESTORE_CCIM_VOLUMES",
        "checksum mismatch",
        "docker volume rm",
        "pg_restore",
        "archive.extractall('/target', filter='data')",
    )
    for expected in restore_requirements:
        _require(restore, expected, location="restore.sh", errors=errors)

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_repository(root)
    if errors:
        print("delivery_contract=invalid")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("delivery_contract=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
