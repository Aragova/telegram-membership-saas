#!/usr/bin/env bash
# ViMi Pay: opt-in, encrypted, append-only GCS backup. Never runs by default.
set -Eeuo pipefail
umask 077

fail() { printf 'BACKUP_FAILED: %s\n' "$*" >&2; exit 1; }
log() { printf 'BACKUP: %s\n' "$*"; }
command -v docker >/dev/null || fail "docker unavailable"
command -v age >/dev/null || fail "age unavailable"
command -v python3 >/dev/null || fail "python3 unavailable"
command -v flock >/dev/null || fail "flock unavailable"
command -v sha256sum >/dev/null || fail "sha256sum unavailable"

[[ "$(id -u)" -eq 0 ]] || fail "run as root with restricted config"
CONFIG="${VIMI_BACKUP_CONFIG:-/etc/vimi-pay/backup.env}"
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || fail "root-owned backup config is missing"
[[ "$(stat -c %u "$CONFIG")" == 0 ]] || fail "backup config must belong to root"
perm="$(stat -c %a "$CONFIG")"
(( (8#$perm & 077) == 0 )) || fail "backup config must not be group/world-readable"
# This is a root-controlled shell config. Never source untrusted files.
# shellcheck source=/dev/null
source "$CONFIG"
: "${VIMI_BACKUP_SESSION_SERVICE_URL:?Set private Cloud Run service URL}"
: "${VIMI_BACKUP_AGE_RECIPIENT:?Set age public recipient}"

# Restrict values to a bucket name and an age recipient; no arbitrary URI
# fragments or path traversal, no injected shell.
[[ "$VIMI_BACKUP_AGE_RECIPIENT" =~ ^age1[a-z0-9]{50,120}$ ]] ||
  fail "invalid age public recipient"

CONTAINER="${VIMI_BACKUP_POSTGRES_CONTAINER:-telegram-membership-saas-staging-postgres-1}"
[[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}$ ]] ||
  fail "invalid container name"
BASE="/var/backups/vimi-pay"
install -d -m 0700 -o root -g root "$BASE"
exec 9>"/run/lock/vimi-pay-backup.lock"
flock -n 9 || fail "another backup already runs"

[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == true ]] ||
  fail "PostgreSQL container is not running"

WORK="$(mktemp -d "$BASE/.gcs-upload.XXXXXXXX")"
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RAND="$(cat /proc/sys/kernel/random/uuid)"
KEY="vimi-pay/postgres/${STAMP}-${RAND}"
DUMP="$WORK/membership.dump.age"
GLOBALS="$WORK/pg-globals.sql.age"
MANIFEST="$WORK/manifest.sha256"

log "streaming encrypted database dump"
# No unencrypted dump or globals ever written to the host filesystem.
docker exec "$CONTAINER" pg_dump -U membership -d membership -Fc -Z 6 |
  age -r "$VIMI_BACKUP_AGE_RECIPIENT" -o "$DUMP"
[[ -s "$DUMP" ]] || fail "empty encrypted database archive"

log "streaming encrypted cluster globals (roles/privileges)"
docker exec "$CONTAINER" pg_dumpall -U membership --globals-only |
  age -r "$VIMI_BACKUP_AGE_RECIPIENT" -o "$GLOBALS"
[[ -s "$GLOBALS" ]] || fail "empty encrypted globals archive"

(
  cd "$WORK"
  sha256sum membership.dump.age pg-globals.sql.age > manifest.sha256
)
log "uploading encrypted database and globals"
# VM needs only a Cloud Run identity token, not GCS read/write OAuth scopes.
# Broker creates GCS resumable sessions under the write-only Backup Writer SA.
# The session URI is a bearer secret: the Python client holds it in memory.
UPLOADER="/usr/local/libexec/vimi-pay/session_upload.py"
[[ -f "$UPLOADER" && ! -L "$UPLOADER" ]] || fail "verified session uploader missing"
[[ "$(stat -c %u "$UPLOADER")" == 0 ]] || fail "session uploader must belong to root"

upload_object() {
  local local_file="$1" part="$2"
  python3 "$UPLOADER" \
    --service-url "$VIMI_BACKUP_SESSION_SERVICE_URL" \
    --prefix "${KEY#vimi-pay/postgres/}" \
    --part "$part" --file "$local_file"
}
upload_object "$DUMP" "membership.dump.age"
upload_object "$GLOBALS" "pg-globals.sql.age"
# Commit marker last. A prefix without manifest is NOT a completed backup.
upload_object "$MANIFEST" "manifest.sha256"

log "BACKUP_UPLOADED: $STAMP (manifest is complete; test restore separately)"
