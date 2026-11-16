#!/usr/bin/env bash
# Runs as root only after backup.service completes, on the SAME VM.
# Never reads bot tokens and never uploads or downloads backup files.
set -Eeuo pipefail
umask 077

case "${SERVICE_RESULT:-}" in
  success) result=success ;;
  *) result=failed ;;
esac

container=telegram-membership-saas-staging-postgres-1
config=/etc/vimi-pay/backup.env
if [[ -e "$config" || -L "$config" ]]; then
  # ExecStart may have REFUSED this file. Validate again before sourcing
  # in the privileged ExecStopPost path. Do not follow any symlink.
  [[ -f "$config" && ! -L "$config" ]] || exit 1
  [[ "$(stat -c %u "$config")" == "0" ]] || exit 1
  perms="$(stat -c %a "$config")"
  (( (8#$perms & 077) == 0 )) || exit 1
  # shellcheck source=/dev/null
  source "$config"
  container="${VIMI_BACKUP_POSTGRES_CONTAINER:-$container}"
fi
[[ "$container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}$ ]] || exit 1

# A host-side receipt proves systemd success/failure, not the existence of a
# cloud object. The central bot ALWAYS verifies the GCS manifest independently.
sql="INSERT INTO public.platform_backup_attempt_receipt (id,reported_at,result)
VALUES (1,clock_timestamp(),'$result')
ON CONFLICT (id) DO UPDATE
SET reported_at=EXCLUDED.reported_at, result=EXCLUDED.result"

docker exec "$container" psql -X -q -v ON_ERROR_STOP=1 \
  -U membership -d membership -c "$sql" >/dev/null

logger -t vimi-pay-backup-report "Backup attempt receipt recorded: $result"
