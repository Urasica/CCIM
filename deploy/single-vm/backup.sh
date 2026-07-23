#!/usr/bin/env bash
set -Eeuo pipefail

age_recipient="${CCIM_BACKUP_AGE_RECIPIENT:-}"
backup_output="${CCIM_BACKUP_OUTPUT:-}"
runtime_env_file="${CCIM_RUNTIME_ENV_FILE:-/etc/ccim/ccim.env}"
state_dir="${CCIM_DEPLOY_STATE_DIR:-/var/lib/ccim-deploy}"
compose_file="${CCIM_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${CCIM_COMPOSE_PROJECT_NAME:-ccim}"

[[ "$age_recipient" == age1* ]] || {
  printf 'backup_status=invalid reason=age_recipient_required\n' >&2
  exit 2
}
[[ "$backup_output" == /* ]] || {
  printf 'backup_status=invalid reason=absolute_backup_output_required\n' >&2
  exit 2
}
[[ ! -e "$backup_output" ]] || {
  printf 'backup_status=invalid reason=backup_output_exists\n' >&2
  exit 2
}
[[ "$state_dir" == /* && "$state_dir" != "/" ]] || {
  printf 'backup_status=invalid reason=unsafe_state_dir\n' >&2
  exit 2
}
[[ -d "$state_dir" ]] || {
  printf 'backup_status=invalid reason=state_dir_missing\n' >&2
  exit 2
}
[[ "$compose_project" =~ ^[a-z0-9][a-z0-9_-]+$ ]] || {
  printf 'backup_status=invalid reason=unsafe_compose_project\n' >&2
  exit 2
}
[[ -f "$runtime_env_file" && -f "$compose_file" ]] || {
  printf 'backup_status=invalid reason=runtime_contract_missing\n' >&2
  exit 2
}

for command_name in age docker python3 tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'backup_status=invalid reason=missing_%s\n' "$command_name" >&2
    exit 2
  }
done

image_ref=""
source_revision=""
state_file="$state_dir/last-known-good.env"
[[ -f "$state_file" ]] || {
  printf 'backup_status=invalid reason=last_known_good_missing\n' >&2
  exit 2
}
while IFS='=' read -r key value; do
  case "$key" in
    CCIM_IMAGE_REF) image_ref="$value" ;;
    CCIM_SOURCE_REVISION) source_revision="$value" ;;
  esac
done < "$state_file"
[[ "$image_ref" =~ @sha256:[0-9a-f]{64}$ ]] || {
  printf 'backup_status=invalid reason=last_known_good_not_digest_pinned\n' >&2
  exit 2
}

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

umask 077
scratch_dir="$(mktemp -d "$state_dir/.backup.XXXXXX")"
backup_temp="${backup_output}.tmp.$$"
services_paused=false
cleanup() {
  if [[ "$services_paused" == true ]]; then
    compose up -d --no-build --wait >/dev/null 2>&1 || true
  fi
  rm -f -- "$backup_temp"
  rm -rf -- "$scratch_dir"
}
trap cleanup EXIT

archive_volume() {
  local volume_name="$1"
  local archive_name="$2"
  docker run --rm \
    --entrypoint python \
    --volume "$volume_name:/source:ro" \
    --volume "$scratch_dir:/backup" \
    "$image_ref" \
    -c "import tarfile; archive=tarfile.open('/backup/$archive_name','w:gz'); archive.add('/source', arcname='.'); archive.close()"
}

mkdir -p "$(dirname "$backup_output")"

compose stop gateway
services_paused=true
compose exec -T postgres pg_dump --username ccim --dbname ccim --format custom \
  > "$scratch_dir/postgres.dump"
compose exec -T redis redis-cli SAVE >/dev/null
compose stop redis

archive_volume "${compose_project}_redis-data" "redis.tar.gz"
archive_volume "${compose_project}_evidence-data" "evidence.tar.gz"

python3 - "$scratch_dir" "$source_revision" "$image_ref" <<'PY'
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

root = Path(sys.argv[1])
source_revision = sys.argv[2]
image_ref = sys.argv[3]
files = ("postgres.dump", "redis.tar.gz", "evidence.tar.gz")
checksums = {
    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
    for name in files
}
payload = {
    "schema": "ccim-encrypted-backup-v1",
    "source_revision": source_revision,
    "image_ref": image_ref,
    "image_digest": image_ref.rsplit("@", 1)[1],
    "created_at": datetime.now(UTC).isoformat(),
    "checksums": checksums,
}
(root / "manifest.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

tar -C "$scratch_dir" -czf - \
  manifest.json postgres.dump redis.tar.gz evidence.tar.gz |
  age --recipient "$age_recipient" --output "$backup_temp"
mv "$backup_temp" "$backup_output"

compose up -d --no-build --wait
services_paused=false
printf 'backup_status=succeeded image_digest=%s\n' "${image_ref##*@}"
