#!/usr/bin/env bash
set -Eeuo pipefail

image_ref="${CCIM_IMAGE_REF:-}"
source_revision="${CCIM_SOURCE_REVISION:-}"
runtime_env_file="${CCIM_RUNTIME_ENV_FILE:-/etc/ccim/ccim.env}"
compose_file="${CCIM_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${CCIM_COMPOSE_PROJECT_NAME:-ccim}"
summary_path="${CCIM_CANARY_SUMMARY_PATH:-artifacts/vm-canary-summary.json}"
delivery_run_id="${GITHUB_RUN_ID:-manual}"

image_ref_pattern='^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$'
source_revision_pattern='^[0-9a-f]{40}$'

[[ "$image_ref" =~ $image_ref_pattern ]] || {
  printf 'canary_status=invalid reason=image_ref_must_be_digest_pinned\n' >&2
  exit 2
}
[[ "$source_revision" =~ $source_revision_pattern ]] || {
  printf 'canary_status=invalid reason=source_revision_must_be_full_sha\n' >&2
  exit 2
}
[[ -f "$runtime_env_file" ]] || {
  printf 'canary_status=invalid reason=runtime_env_file_missing\n' >&2
  exit 2
}

for command_name in docker python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'canary_status=invalid reason=missing_%s\n' "$command_name" >&2
    exit 2
  }
done

mkdir -p "$(dirname "$summary_path")"
scratch_dir="$(mktemp -d)"
trap 'rm -rf -- "$scratch_dir"' EXIT
dry_run_path="$scratch_dir/operational-readiness.json"
migration_path="$scratch_dir/migration-state.json"

compose() {
  CCIM_IMAGE_REF="$image_ref" \
    CCIM_BUILD_REF="$source_revision" \
    CCIM_RUNTIME_ENV_FILE="$runtime_env_file" \
    docker compose \
      --project-name "$compose_project" \
      --env-file "$runtime_env_file" \
      --file "$compose_file" \
      "$@"
}

compose exec -T gateway \
  python -m ccim.operations dry-run --json > "$dry_run_path"
compose exec -T gateway \
  python -m ccim.migrations check --json > "$migration_path"

python3 - \
  "$dry_run_path" \
  "$migration_path" \
  "$summary_path" \
  "$delivery_run_id" \
  "$source_revision" \
  "${image_ref##*@}" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

(
    dry_run_path,
    migration_path,
    output_path,
    delivery_run_id,
    source_revision,
    image_digest,
) = sys.argv[1:]

dry_run = json.loads(Path(dry_run_path).read_text(encoding="utf-8"))
migration = json.loads(Path(migration_path).read_text(encoding="utf-8"))
data = dry_run.get("data", {})
provider = data.get("provider", {})

if dry_run.get("ok") is not True:
    raise SystemExit("operational dry-run did not report ok=true")
if provider.get("network_calls") != 0 or provider.get("external_provider_calls") != 0:
    raise SystemExit("VM dry-run attempted an external provider call")
if migration.get("current") is not True:
    raise SystemExit("VM migration state is not current")

payload = {
    "schema": "ccim-vm-canary-dry-run-v1",
    "report_label": "dry-run",
    "actual_data": False,
    "delivery_run_id": delivery_run_id,
    "deployment_sha": source_revision,
    "image_digest": image_digest,
    "migration": migration,
    "provider": provider,
    "simulated_utc_ledger": data.get("simulated_utc_ledger"),
    "budget_checks": data.get("budget_checks"),
    "recorded_at": datetime.now(UTC).isoformat(),
}
Path(output_path).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

PYTHONPATH=src python3 scripts/check_artifact_safety.py "$summary_path"
printf 'canary_status=succeeded image_digest=%s source_revision=%s\n' \
  "${image_ref##*@}" "$source_revision"
