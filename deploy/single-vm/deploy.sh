#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:-deploy}"
image_ref="${CCIM_IMAGE_REF:-}"
source_revision="${CCIM_SOURCE_REVISION:-}"
runtime_env_file="${CCIM_RUNTIME_ENV_FILE:-/etc/ccim/ccim.env}"
state_dir="${CCIM_DEPLOY_STATE_DIR:-/var/lib/ccim-deploy}"
compose_file="${CCIM_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${CCIM_COMPOSE_PROJECT_NAME:-ccim}"
summary_path="${CCIM_DEPLOY_SUMMARY_PATH:-artifacts/deployment-summary.json}"
delivery_run_id="${GITHUB_RUN_ID:-manual}"

image_ref_pattern='^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$'
source_revision_pattern='^[0-9a-f]{40}$'

fail_contract() {
  printf 'delivery_contract=invalid reason=%s\n' "$1" >&2
  exit 2
}

[[ "$image_ref" =~ $image_ref_pattern ]] || fail_contract "image_ref_must_be_digest_pinned"
[[ "$source_revision" =~ $source_revision_pattern ]] || fail_contract "source_revision_must_be_full_sha"
[[ "$state_dir" == /* && "$state_dir" != "/" ]] || fail_contract "state_dir_must_be_safe_absolute_path"
[[ -n "$compose_file" ]] || fail_contract "compose_file_required"

if [[ "$mode" == "--validate-only" ]]; then
  printf 'delivery_contract=ok image_digest=%s source_revision=%s\n' \
    "${image_ref##*@}" "$source_revision"
  exit 0
fi
[[ "$mode" == "deploy" ]] || fail_contract "unsupported_mode"

for command_name in docker curl flock python3; do
  command -v "$command_name" >/dev/null 2>&1 || fail_contract "missing_${command_name}"
done
[[ -f "$runtime_env_file" ]] || fail_contract "runtime_env_file_missing"
[[ -f "$compose_file" ]] || fail_contract "compose_file_missing"

mkdir -p "$state_dir" "$(dirname "$summary_path")"
umask 077
exec 9>"$state_dir/deploy.lock"
flock -n 9 || fail_contract "deployment_already_running"

previous_image_ref=""
previous_source_revision=""
state_file="$state_dir/last-known-good.env"
if [[ -f "$state_file" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      CCIM_IMAGE_REF) previous_image_ref="$value" ;;
      CCIM_SOURCE_REVISION) previous_source_revision="$value" ;;
    esac
  done < "$state_file"
fi

failure_step=""

compose_for() {
  local active_ref="$1"
  local active_revision="$2"
  shift 2
  CCIM_IMAGE_REF="$active_ref" \
    CCIM_BUILD_REF="$active_revision" \
    CCIM_RUNTIME_ENV_FILE="$runtime_env_file" \
    docker compose \
      --project-name "$compose_project" \
      --env-file "$runtime_env_file" \
      --file "$compose_file" \
      "$@"
}

deploy_ref() {
  local candidate_ref="$1"
  local candidate_revision="$2"
  local observed_revision

  failure_step="image_pull"
  docker pull "$candidate_ref" || return 1

  failure_step="revision_label"
  observed_revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$candidate_ref"
  )" || return 1
  [[ "$observed_revision" == "$candidate_revision" ]] || return 1

  failure_step="compose_migration_or_readiness"
  compose_for "$candidate_ref" "$candidate_revision" up -d --no-build --wait || return 1

  failure_step="live_probe"
  curl --fail --silent --show-error http://127.0.0.1:8080/live >/dev/null || return 1

  failure_step="ready_probe"
  curl --fail --silent --show-error http://127.0.0.1:8080/ready >/dev/null || return 1

  failure_step=""
  return 0
}

write_summary() {
  local deployment_status="$1"
  local rollback_status="$2"
  local failed_step="$3"

  python3 - \
    "$summary_path" \
    "$delivery_run_id" \
    "$source_revision" \
    "$image_ref" \
    "$previous_image_ref" \
    "$deployment_status" \
    "$rollback_status" \
    "$failed_step" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

(
    output,
    delivery_run_id,
    source_revision,
    image_ref,
    previous_image_ref,
    deployment_status,
    rollback_status,
    failed_step,
) = sys.argv[1:]

payload = {
    "schema": "ccim-single-vm-deployment-v1",
    "delivery_run_id": delivery_run_id,
    "source_revision": source_revision,
    "image_ref": image_ref,
    "image_digest": image_ref.rsplit("@", 1)[1],
    "previous_image_ref": previous_image_ref or None,
    "deployment_status": deployment_status,
    "rollback_status": rollback_status,
    "failure_step": failed_step or None,
    "recorded_at": datetime.now(UTC).isoformat(),
}
Path(output).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

if deploy_ref "$image_ref" "$source_revision"; then
  next_state="$(mktemp "$state_dir/.last-known-good.XXXXXX")"
  {
    printf 'CCIM_IMAGE_REF=%s\n' "$image_ref"
    printf 'CCIM_SOURCE_REVISION=%s\n' "$source_revision"
  } > "$next_state"
  mv "$next_state" "$state_file"
  write_summary "succeeded" "not_needed" ""
  printf 'deployment_status=succeeded image_digest=%s source_revision=%s\n' \
    "${image_ref##*@}" "$source_revision"
  exit 0
fi

candidate_failure_step="$failure_step"
rollback_status="not_available"
if [[ "$previous_image_ref" =~ $image_ref_pattern ]] \
  && [[ "$previous_source_revision" =~ $source_revision_pattern ]] \
  && [[ "$previous_image_ref" != "$image_ref" ]]; then
  if deploy_ref "$previous_image_ref" "$previous_source_revision"; then
    rollback_status="succeeded"
  else
    rollback_status="failed"
    candidate_failure_step="${candidate_failure_step};rollback_${failure_step}"
  fi
fi

write_summary "failed" "$rollback_status" "$candidate_failure_step"
printf 'deployment_status=failed rollback_status=%s failure_step=%s\n' \
  "$rollback_status" "$candidate_failure_step" >&2
exit 1
