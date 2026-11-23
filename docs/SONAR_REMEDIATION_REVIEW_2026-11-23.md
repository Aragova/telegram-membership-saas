# SonarQube remediation: validated against ViMi Pay runtime (2026-11-23)

## Scope and provenance

Reviewed the supplied SonarQube Cloud API export for `main`: 28 historical
findings, of which 19 were `OPEN` and 9 already `FIXED`. Findings were
cross-checked against the **current** repository code, related call sites,
tests, container manifests and the opt-in backup deployment script.
The export is a point-in-time snapshot, not a guarantee of current Sonar status.

This PR changes no SQL migrations, business schema, payment logic, Telegram
permissions or production credentials. It must not be deployed automatically.

## Dependency tracing before changes

- `ops/backup/vimi-pay-gcs-backup.sh` creates a root-owned temporary workdir
  `/var/backups/vimi-pay/.gcs-upload.XXXXXXXX`; `age` encrypts
  `membership.dump.age` and `pg-globals.sql.age`, and the SHA-256 manifest
  is uploaded last. It calls `session_upload.py` with a controlled
  `--prefix`, `--part`, and `--file`.
- `session_upload.request_session` uses GCE metadata to obtain a Cloud Run
  identity token, then accesses the IAM-protected broker. Existing functions
  already validate HTTPS broker origins, disable HTTP redirects and restrict
  GCS upload origins. This PR **reinforces those boundaries**, rather than
  implementing a second backup protocol.
- `session_broker.start_gcs_session` creates a write-only GCS resumable
  session under the backup bucket. Its HTTP listener is **internal Cloud Run
  ingress**, not a public HTTP endpoint; Cloud Run supplies external TLS
  termination and IAM authentication. Do not change the listener to
  `127.0.0.1`: Cloud Run must reach it on the container interface.
- `task_lifecycle.complete_cleanup` is used inside `finally` blocks by
  the central bot, customer bot, supervisor and engine cleanup. It must
  finish Telegram and database teardown despite repeated cancellations,
  *before* advisory locks are released. This PR retains shielding and
  now explicitly propagates a new cancellation **after** cleanup finishes.
- `SharedProjectLeaseManager._finish_critical` deliberately catches
  cancellation to finish serialized SQL on one shared PostgreSQL connection,
  returning a cancellation flag. Its callers re-raise after acquisition,
  release, execution or confirmation; the existing 100-bot lease/RLS tests
  check ownership and no cross-project unlock. Do not replace with a direct
  re-raise inside the SQL critical section.
- `owner_setup.dashboard_ui._channel_rows` intentionally emits one row for
  channel-received Stars or two rows for bot/mixed recipients. The already
  existing `test_sonar_hardening.py` protects this UX invariant. This PR
  preserves the data and uses a single return expression instead of creating
  an additional UI abstraction.
- `requirements-runtime.lock`, `requirements-build.lock`,
  `requirements-dev.lock`, and `requirements-quality.lock` already pin
  dependencies with SHA-256. Do not introduce a competing lock system.

## Disposition of each OPEN finding group

| Report finding(s) | Disposition and risk decision |
|---|---|
| `python:S8495` dashboard rows (1) | Refactored one return shape; 1-vs-2 *visible* rows unchanged |
| `pythonsecurity:S8703` broker SSRF (1) | Validate HTTPS `.run.app` URL in `request_session` itself before obtaining or transmitting IAM token |
| `pythonsecurity:S5144` GCS session SSRF (1) | Require exact GCS HTTPS host and documented upload path/query; broker also binds response path to configured bucket |
| `pythonsecurity:S8707` local backup file (1) | Restrict to encrypted root-owned backup workdir and the three fixed file names; disallow symlinks and non-regular files |
| `python:S7497` cleanup (1) | Re-raise late cancellation after cleanup, with dedicated tests |
| `python:S7497` shared lease (1) | Intentional deferred cancellation; existing caller re-raises and integration tests verify lock integrity. Do **not** suppress cancellation inside a running SQL statement |
| `githubactions:S8541/S8544` CI pip upgrade (2) | Remove unpinned `pip install --upgrade pip`. Remaining dependencies already use hashed wheel-only locks |
| `python:S5332` Cloud Run HTTP listener (1) | Expected internal HTTP behind Cloud Run TLS/IAM; deployment security control, not a request to expose plaintext HTTP publicly |
| `text:S8565` dependency lock (1) | Scanner does not recognize the existing fully pinned `requirements-*.lock` files; `tests/test_sonar_hardening.py` checks their integrity |
| `secrets:S6698` CI/Compose (7), `yaml:S2068` CI/Compose (2) | **Confirmed live risk on staging**: both public `membership` (SUPERUSER) and `membership_runtime` passwords authenticate over TCP. Compose and first-boot SQL now require private credentials. Disposable CI values are intentional; active database still requires a controlled operator rotation. See [rotation runbook](POSTGRES_CREDENTIAL_ROTATION.md). |

## Verified live PostgreSQL findings (23 Nov 2026)

The operator executed read-only Docker/PostgreSQL diagnostics on the VM:

- TCP login with both **published** passwords was accepted; an intentionally
  invalid password was rejected. Thus these are **real active credentials**,
  not a Sonar false positive and not a merely deprecated Docker env value.
- `membership` is SUPERUSER; `membership_runtime` is a non-superuser.
- DB host port bindings: `{}` (no direct host-port exposure).
- Active connections: 1 `membership`, 8 `membership_runtime`,
  1 separate `vimi_shortlink_api`. Bot and supervisor must be updated
  together; public API has independent secrets but shares the same DB host.
- Last automatic encrypted upload **23 Nov 2026, 03:23 UTC** succeeded;
  recent daily uploads succeeded. A previous isolated restore on
  **16 Nov 2026** was successful; a restore of the latest copy has not
  been independently tested.
- Host `.env`: `0600`, owned by VM user; backup.env: root-owned `0600`.
  File permissions alone cannot compensate for published DB passwords.

The previous hypothesis that these were merely unused development examples
is conclusively **false for the staging server**.

### Prepared fix and remaining operational gate

The PR removes published passwords from runtime Compose and role
initialization SQL, adds a private first-boot role-password hook and tests
an isolated PostgreSQL 16 container against old and new credentials.
**It intentionally does not change credentials on the live VM.**

Do not declare the security incident closed until both real role passwords
are rotated, the public values fail TCP authentication, the bot/supervisor
are healthy, the PostgreSQL metadata no longer exposes a useful old
bootstrap secret, the API is healthy and the backup/upload still works.
All prerequisites, ordered actions and rollback decisions are in
[docs/POSTGRES_CREDENTIAL_ROTATION.md](POSTGRES_CREDENTIAL_ROTATION.md).

CI-only test accounts and intentionally disposable passwords remain separate
from staging. Their Sonar disposition must be recorded as *test-only*, not
generalized to the real database.

## Regression and review gates

- `tests/test_backup_release_artifacts.py`: forbid broker SSRF before network,
  constrain local encrypted file paths, refuse symlinks/other buckets and
  stream only to the Google Storage origin.
- `tests/test_task_lifecycle_cancellation.py`: verify multiple cancellations
  do not abandon cleanup; propagate cancel after cleanup; retain failures.
- Existing `tests/test_customer_supervisor.py`,
  `tests/test_shared_supervisor_leases_rls.py` and
  `tests/test_sonar_hardening.py` continue guarding real worker/Stars behavior.
- GitHub CI, SonarQube, CodeQL, CodeRabbit and Codex must review exact PR HEAD.
  No merge or live deployment is part of this PR preparation.

**Sonar status is not a synonym for security.** Any alert remaining after
this PR needs evidence-based disposition, not a superficial `NOSONAR`
comment or architectural rewrite solely to make a counter reach zero.
