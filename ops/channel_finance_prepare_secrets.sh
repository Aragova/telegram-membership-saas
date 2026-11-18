#!/usr/bin/env bash
# Root-only, opt-in provisioning for ONE Telegram-owner finance sidecar.
# Docker Compose file-backed secrets keep original host UID/mode. This uses
# mode 0400 and a DEDICATED non-root container UID, never mode 0444.
set -Eeuo pipefail
umask 077

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root on the explicitly approved Docker host." >&2
  exit 1
fi
if [[ "$#" -ne 4 ]]; then
  echo "Usage: $0 API_HASH_SOURCE SESSION_SOURCE ACCESS_KEY_SOURCE DEST_DIR" >&2
  exit 2
fi
hash_source="$1"
session_source="$2"
key_source="$3"
destination="$4"

if [[ "${destination}" != "/srv/vimi-channel-finance-secrets" ]]; then
  echo "Destination must be the dedicated finance secret directory." >&2
  exit 2
fi
for source in "$hash_source" "$session_source" "$key_source"; do
  if [[ ! -f "$source" || -L "$source" || ! -s "$source" ]]; then
    echo "Missing, symlinked or empty finance source secret." >&2
    exit 1
  fi
done
if [[ -L "$destination" ]]; then
  echo "Finance destination must not be a symlink." >&2
  exit 1
fi

# UID 25073 belongs ONLY to the isolated reader. Never run it as root and
# never world-enable session files: docker-compose file secrets ignore uid/mode
# settings and retain the host file's original ownership and permissions.
install -d -o root -g root -m 0700 "$destination"
install -o 25073 -g 25073 -m 0400 "$hash_source" \
  "$destination/channel_finance_api_hash"
install -o 25073 -g 25073 -m 0400 "$session_source" \
  "$destination/channel_finance_session"
install -o 25073 -g 25073 -m 0400 "$key_source" \
  "$destination/channel_finance_access_key"

for name in channel_finance_api_hash channel_finance_session channel_finance_access_key; do
  uid="$(stat -c %u "$destination/$name")"
  mode="$(stat -c %a "$destination/$name")"
  if [[ "$uid" != "25073" || "$mode" != "400" ]]; then
    echo "Refusing incorrectly owned or readable finance secret." >&2
    exit 1
  fi
done
echo "Private finance secrets prepared for non-root UID 25073."
echo "Use this directory for CHANNEL_FINANCE_*_FILE paths in the opt-in overlay."
