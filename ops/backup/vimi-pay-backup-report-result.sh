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
if [[ -r /etc/vimi-pay/backup.env ]]; then
  # Root-owned 0600 file is already validated by the backup script.
  # shellcheck source=/dev/null
  source /etc/vimi-pay/backup.env
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
