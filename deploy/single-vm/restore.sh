#!/usr/bin/env bash
set -Eeuo pipefail

backup_input="${CCIM_BACKUP_INPUT:-}"
age_identity_file="${CCIM_BACKUP_AGE_IDENTITY_FILE:-}"
restore_confirm="${CCIM_RESTORE_CONFIRM:-}"
runtime_env_file="${CCIM_RUNTIME_ENV_FILE:-/etc/ccim/ccim.env}"
state_dir="${CCIM_DEPLOY_STATE_DIR:-/var/lib/ccim-deploy}"
compose_file="${CCIM_COMPOSE_FILE:-docker-compose.yml}"
compose_project="${CCIM_COMPOSE_PROJECT_NAME:-ccim}"
summary_path="${CCIM_RESTORE_SUMMARY_PATH:-artifacts/restore-summary.json}"

[[ "$restore_confirm" == "RESTORE_CCIM_VOLUMES" ]] || {
  printf 'restore_status=blocked reason=explicit_confirmation_required\n' >&2
  exit 2
}
[[ "$backup_input" == /* && -f "$backup_input" ]] || {
  printf 'restore_status=invalid reason=encrypted_backup_missing\n' >&2
  exit 2
}
[[ "$age_identity_file" == /* && -f "$age_identity_file" ]] || {
  printf 'restore_status=invalid reason=age_identity_missing\n' >&2
  exit 2
}
[[ "$state_dir" == /* && "$state_dir" != "/" ]] || {
  printf 'restore_status=invalid reason=unsafe_state_dir\n' >&2
  exit 2
}
[[ -d "$state_dir" ]] || {
  printf 'restore_status=invalid reason=state_dir_missing\n' >&2
  exit 2
}
[[ "$compose_project" =~ ^[a-z0-9][a-z0-9_-]+$ ]] || {
  printf 'restore_status=invalid reason=unsafe_compose_project\n' >&2
  exit 2
}
[[ -f "$runtime_env_file" && -f "$compose_file" ]] || {
  printf 'restore_status=invalid reason=runtime_contract_missing\n' >&2
  exit 2
}

for command_name in age curl docker python3 tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'restore_status=invalid reason=missing_%s\n' "$command_name" >&2
    exit 2
  }
done

umask 077
scratch_dir="$(mktemp -d "$state_dir/.restore.XXXXXX")"
trap 'rm -rf -- "$scratch_dir"' EXIT
age --decrypt --identity "$age_identity_file" "$backup_input" |
  tar -C "$scratch_dir" -xzf -

readarray -t manifest_values < <(
  python3 - "$scratch_dir" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("schema") != "ccim-encrypted-backup-v1":
    raise SystemExit("unsupported backup schema")
image_ref = str(manifest.get("image_ref", ""))
source_revision = str(manifest.get("source_revision", ""))
if not re.fullmatch(r"[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}", image_ref):
    raise SystemExit("backup image is not digest pinned")
if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
    raise SystemExit("backup source revision is invalid")
required = {"postgres.dump", "redis.tar.gz", "evidence.tar.gz"}
checksums = manifest.get("checksums", {})
if set(checksums) != required:
    raise SystemExit("backup member set is incomplete or unexpected")
for name, expected in checksums.items():
    actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"checksum mismatch: {name}")
print(image_ref)
print(source_revision)
PY
)
image_ref="${manifest_values[0]}"
source_revision="${manifest_values[1]}"

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

restore_volume() {
  local volume_name="$1"
  local archive_name="$2"
  docker run --rm \
    --entrypoint python \
    --volume "$volume_name:/target" \
    --volume "$scratch_dir:/backup:ro" \
    "$image_ref" \
    -c "import tarfile; archive=tarfile.open('/backup/$archive_name','r:gz'); archive.extractall('/target', filter='data'); archive.close()"
}

docker pull "$image_ref"
observed_revision="$(
  docker image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$image_ref"
)"
[[ "$observed_revision" == "$source_revision" ]]

compose down --remove-orphans
for volume_name in \
  "${compose_project}_postgres-data" \
  "${compose_project}_redis-data" \
  "${compose_project}_evidence-data"; do
  docker volume inspect "$volume_name" >/dev/null 2>&1 &&
    docker volume rm "$volume_name" >/dev/null
done
compose create --no-build

restore_volume "${compose_project}_redis-data" "redis.tar.gz"
restore_volume "${compose_project}_evidence-data" "evidence.tar.gz"

compose up -d --wait postgres
compose exec -T postgres pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --username ccim \
  --dbname ccim < "$scratch_dir/postgres.dump"

compose up -d --no-build --wait
curl --fail --silent --show-error http://127.0.0.1:8080/live >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/ready >/dev/null

next_state="$(mktemp "$state_dir/.last-known-good.XXXXXX")"
{
  printf 'CCIM_IMAGE_REF=%s\n' "$image_ref"
  printf 'CCIM_SOURCE_REVISION=%s\n' "$source_revision"
} > "$next_state"
mv "$next_state" "$state_dir/last-known-good.env"

mkdir -p "$(dirname "$summary_path")"
python3 - "$summary_path" "$source_revision" "$image_ref" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

output, source_revision, image_ref = sys.argv[1:]
payload = {
    "schema": "ccim-restore-summary-v1",
    "source_revision": source_revision,
    "image_ref": image_ref,
    "image_digest": image_ref.rsplit("@", 1)[1],
    "restore_status": "succeeded",
    "live": "pass",
    "ready": "pass",
    "recorded_at": datetime.now(UTC).isoformat(),
}
Path(output).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
PYTHONPATH=src python3 scripts/check_artifact_safety.py "$summary_path"
printf 'restore_status=succeeded image_digest=%s\n' "${image_ref##*@}"
