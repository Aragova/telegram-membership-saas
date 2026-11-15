#!/usr/bin/env bash
# ViMi Pay: opt-in, encrypted, append-only GCS backup. Never runs by default.
set -Eeuo pipefail
umask 077

fail() { printf 'BACKUP_FAILED: %s\n' "$*" >&2; exit 1; }
log() { printf 'BACKUP: %s\n' "$*"; }
command -v docker >/dev/null || fail "docker unavailable"
command -v age >/dev/null || fail "age unavailable"
command -v gcloud >/dev/null || fail "gcloud CLI unavailable"
command -v curl >/dev/null || fail "curl unavailable"
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
: "${VIMI_BACKUP_GCS_BUCKET:?Set destination bucket name}"
: "${VIMI_BACKUP_AGE_RECIPIENT:?Set age public recipient}"

# Restrict values to a bucket name and an age recipient; no arbitrary URI
# fragments or path traversal, no injected shell.
[[ "$VIMI_BACKUP_GCS_BUCKET" =~ ^[a-z0-9][a-z0-9._-]{2,61}[a-z0-9]$ ]] ||
  fail "invalid bucket name"
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
# The JSON API uploads to an EXACT object name via objects.insert, avoiding
# 'storage.objects.list' permission needed by gcloud storage cp object paths.
# Only create permission is granted to the backup VM identity. The bucket and
# prefix are validated/constructed here; only '/' needs percent-encoding.
upload_object() {
  local local_file="$1" object="$2" encoded token
  [[ "$object" =~ ^[a-zA-Z0-9_./-]+$ ]] || fail "invalid object name"
  encoded="${object//\//%2F}"
  # Fetch short-lived OAuth credentials in memory. Never put a bearer token
  # on curl argv or in log files; pass the header by an anonymous FD instead.
  token="$(gcloud auth print-access-token)" || fail "GCE credential unavailable"
  [[ -n "$token" ]] || fail "empty GCE access token"
  curl --fail --silent --show-error \
    --connect-timeout 10 --max-time 1800 \
    -X POST --data-binary "@$local_file" \
    -H "Content-Type: application/octet-stream" \
    -H @<(printf 'Authorization: Bearer %s\n' "$token") \
    -o /dev/null \
    "https://storage.googleapis.com/upload/storage/v1/b/$VIMI_BACKUP_GCS_BUCKET/o?uploadType=media&name=$encoded&ifGenerationMatch=0"
  unset token
}
upload_object "$DUMP" "$KEY/membership.dump.age"
upload_object "$GLOBALS" "$KEY/pg-globals.sql.age"
# Commit marker last. A prefix without manifest is NOT a completed backup.
upload_object "$MANIFEST" "$KEY/manifest.sha256"

log "BACKUP_UPLOADED: $STAMP (manifest is complete; test restore separately)"
