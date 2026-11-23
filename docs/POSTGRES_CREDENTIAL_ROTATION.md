# ViMi Pay — PostgreSQL credential hardening and staged rotation

## Verified on staging VM, 23 Nov 2026

**This is an actionable incident, not a test-only Sonar false positive.**

- Compose project: `telegram-membership-saas-staging`, at
  `/home/chaneloberlin819/telegram-membership-saas-staging/compose.yaml`.
- Database container: `telegram-membership-saas-staging-postgres-1`.
  PostgreSQL **does not publish host ports**; the Docker network also contains
  `bot`, `customer-supervisor` and the separate `vimi-pay-public/pay-api`.
- Both `membership` (database owner and superuser) and `membership_runtime`
  (non-superuser) **authenticate over TCP using public source-controlled
  passwords**. Wrong-password probes failed, so this was not merely `trust`.
- Active connections: 1 owner, 8 runtime and 1 `vimi_shortlink_api`
  (the API uses its separate, independently configured role).
- The backup timer is active. Latest upload on 23 Nov 2026 at 03:23 UTC
  succeeded, with successful daily uploads recorded over the preceding week.
  A genuine isolated encrypted restore including roles/ACL was performed on
  16 Nov 2026 (see `docs/BACKUP_RECOVERY.md`). **No restore test was done
  on the 23 Nov copy**, and full recovery of VM secrets is not certified.
- Host `.env` permissions: `0600`, owner `chaneloberlin819`;
  `/etc/vimi-pay/backup.env`: `0600`, owner `root`.

## What this PR changes

1. `compose.yaml` no longer publishes role passwords. It **requires**
   `VIMI_PG_OWNER_PASSWORD` and `VIMI_PG_RUNTIME_PASSWORD` from
   a **gitignored host `.env`** and constructs existing application URLs
   from them. Bot and supervisor stay on `membership_runtime`; migration
   owner credentials remain separate from runtime operations.
2. The **first-boot only** SQL creates `membership_runtime` without a
   published password; the additional `20-runtime-password.sh` hook sets
   a private password. Both secrets must use URL-safe letters, digits,
   underscores and hyphens, as they are included directly in PostgreSQL URLs.
3. CI supplies deliberately disposable testing values; no live server
   credentials are present in GitHub Actions.
4. The initialisation hook **does not run** when the PostgreSQL Docker
   volume already has data. Existing roles must be rotated separately.

**Merging this PR does not rotate the staging passwords.** Do not run
the ordinary deployment workflow before configuring the host secrets
and completing the role-rotation plan. A routine deploy without them will
fail closed; recreating a DB container with mismatched credentials can
make bot reconnections fail.

## Rotation prerequisites (manual, scheduled maintenance only)

- Schedule a maintenance window; **brief unavailability of bot purchases,
  polling and API/database reconnects is possible**. No zero-downtime claim.
- Confirm recent complete GCS backup and access to the separate age
  private key; keep a verified encrypted rollback copy outside the VM.
- Confirm the existing deployment compose overlays/profile parameters,
  all running bot instances and the API database role. Avoid blindly running
  `docker compose down -v` or `docker volume rm`: both can destroy data.
- Preserve a root-readable `0600` copy of the existing `.env` **outside
  Git**, and never print passwords/connection URLs in terminal logs, CI,
  support chats or review comments.
- Generate **two distinct** cryptographically random URL-safe passwords
  on the VM using `secrets.token_urlsafe(36)`, enter them only in the host
  `.env` as `VIMI_PG_OWNER_PASSWORD` and `VIMI_PG_RUNTIME_PASSWORD`.
  The operator must inspect the new keys' presence without echoing values.
- Validate `docker compose -p telegram-membership-saas-staging
  -f compose.yaml -f compose.light100.yaml --profile telegram
  --profile supervisor config -q` using the same overlays as the
  current deployment. Inspect the **config source labels** of both
  `bot` and `customer-supervisor` first to confirm the actual overlays.
- Run the **approved CI-tested commit** and compare it against current
  `main` before starting a maintenance window.

## Rotation sequence to execute only after operational approval

1. Prebuild the desired images if the release changes code; avoid Docker
   builds or dependency downloads during the brief planned outage.
2. Gracefully stop or quiesce the **central bot** and **customer-supervisor**
   so ongoing payment handlers and project leases finish before credential
   changes. Keep PostgreSQL data volume intact.
3. Change both actual PostgreSQL role passwords through an authenticated
   **local administrative session**, using parameterized or safely quoted
   SQL sent through **stdin**, not SQL literals printed in shell history,
   command-line arguments or logs. The `ALTER ROLE` statements alone do
   not modify any business table or schema.
4. Recreate the two bot services **using the reviewed Compose configuration
   with private secrets**; verify role connectivity and no error spikes,
   active polling and advisory-lock ownership before resuming purchases.
5. The existing PostgreSQL container may still retain its obsolete
   `POSTGRES_PASSWORD` environment value after `ALTER ROLE`.
   A deliberate **PostgreSQL container recreation**, without deleting
   the named data volume, is needed to update its container metadata.
   This interrupts API and bot DB connections, so perform it under the
   *same maintenance plan*, then verify auto-reconnection and health.
6. The `pay-api` uses `vimi_shortlink_api` and a separate credential.
   It does **not** need its password changed in this rotation, but its
   endpoint must be health-checked after any PostgreSQL restart.
7. Verify via independent TCP authentication from inside the DB container:
   **both former public passwords must fail, both privately configured
   passwords must succeed, and the DB must still have zero published host
   ports**. Do not print the private values.
8. Check `pg_stat_activity` roles, active customer bots, payments and
   access queues, monitoring, and the next encrypted backup. Verify the
   backup reporter can still connect using Unix socket local auth. If the
   health checks fail, stop progression and follow the **approved** rollback
   protocol; do not blindly repeat `ALTER ROLE` or reset data volumes.

### Rollback and critical warnings

- An environment-file backup by itself is **not** a DB credential rollback:
  once the SQL role has changed, the password in PostgreSQL has changed.
- Before executing, keep a controlled recovery procedure for restoring the
  previous role credentials **and** matching Compose environment, and a
  separate tested database recovery path. Rollback to known public secrets
  is an emergency temporary measure, not a resolution.
- The existing backup unit calls local `pg_dump`/`pg_dumpall` via a
  Unix socket; confirm their operation after rotating the superuser.
- **No secret rotation is implemented as an automatic GitHub action**.
  This runbook is deliberately not an unattended production-change script.
- This host currently identifies the running bot and supervisor containers
  as `TMS_ENVIRONMENT=development`, even though the Compose project is
  named staging. This pre-existing mismatch should be assessed separately;
  do not silently change unrelated runtime behaviour during rotation.

## Completion criteria

1. Both private passwords verified on live PostgreSQL.
2. Both old public passwords rejected over the same TCP authentication path.
3. Role privilege separation and API connections preserved.
4. Real access, billing and customer-supervisor smoke scenarios passed.
5. Backup timer and a fresh encrypted upload validated.
6. Only after evidence is recorded, resolve the associated **existing
   `main` Sonar findings**, not merely the PR's “0 new issues” status.
