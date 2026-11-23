# SonarQube Cloud main: remaining eight findings (23 November 2026)

Source: operator-supplied SonarQube `api/issues/search` JSON for
`Aragova_telegram-membership-saas`, `branch=main`, OPEN/CONFIRMED.
`total=8`; last-update timestamps on individual findings were
19 November 2026. Cross-checked against main commit
`d42ab1a0a8082d7915a256fb825bd9eaf004fde8`.
The Sonar main Quality Gate and GitHub CI passed, but that **does not**
mean there are no open historical issues.

## Findings and disposition

| Issue key | Rule and location | Assessment / action |
|---|---|---|
| `AaH1CVK0J8YxpWOrqt6r` | `secrets:S6698`, `.github/workflows/ci.yml` line 23 | **Fix in this PR.** Old literal runtime database URL was scoped to ephemeral GitHub Actions PG, not staging. Replace with per-run/attempt value used consistently for role creation and tests. No live VM DB credentials are involved. |
| `AaH1CVK0J8YxpWOrqt6q` | `yaml:S2068`, `.github/workflows/ci.yml` line 31 | **Fix in this PR.** Replace disposable PG service's static password with same per-run owner password, and use it for Alembic/asyncpg setup. |
| `AaH1CVKkJ8YxpWOrqt6i` | `pythonsecurity:S8703`, `ops/backup/session_upload.py` line 90 | **Protected by PR #229, request review of issue status.** `request_session` now calls `validate_service_url` before requesting Google ID token; requires HTTPS, exact `.run.app` domain suffix, no userinfo/nonstandard port/path/query/fragment. Explicitly disables HTTP redirects carrying IAM token. Input is a root-owned backup configuration, not a public user endpoint. Keep tests for malformed origins and redirects. |
| `AaH1CVKkJ8YxpWOrqt6h` | `pythonsecurity:S5144`, `ops/backup/session_upload.py` line 150 | **Protected by PR #229, request review of issue status.** `stream_put` requires HTTPS and exact `storage.googleapis.com` host/port and GCS upload path, no userinfo/fragment, validated upload ID. The connection host is a constant, not taken from the response; local source is limited to encrypted backup working files without symlinks. Tests exercise these boundaries. |
| `AaH1CVJmJ8YxpWOrqt6f` | `python:S7497`, `src/telegram_membership_saas/task_lifecycle.py` line 26 | **Fixed behavior in PR #229, scanner still flags intentional protected cleanup.** Repeated cancellation is remembered and re-raised after cleanup task finishes; tests verify no premature exit. Do not re-raise from inside the `except` before cleanup. |
| `AaH1CVJjJ8YxpWOrqt6e` | `python:S7497`, `src/telegram_membership_saas/project_worker_lock.py` line 173 | **Intentional lease safety; document acceptance.** A shared PostgreSQL connection must not cancel a running statement or release the mutex prematurely. `_finish_critical` finishes the shielded SQL task and reports the cancellation flag. Every caller (lease acquire/release, execute, confirm) then re-raises; integration tests verify no cross-project or duplicate leases. |
| `AaH1CVKvJ8YxpWOrqt6k` | `python:S5332`, `ops/backup/session_broker/server.py` line 122 | **Intentional transport architecture; validate deployment and mark accepted only with evidence.** HTTP listener is private **inside Cloud Run**; Google front end terminates HTTPS and IAM `run.invoker` gates the service. Binding to loopback or serving HTTPS inside the container would be incorrect for this deployment. Keep Cloud Run authenticated and non-public. |
| `AaH1CVKxJ8YxpWOrqt6l` | `text:S8565`, `pyproject.toml` | **Scanner does not recognize pinned lock files.** Runtime, dev, quality and build requirements are SHA-256 pinned in `requirements-*.lock`, installed with `--require-hashes --only-binary=:all:` by Dockerfile and CI. Existing tests assert pins/hashes. Do not introduce a second incompatible lock system just to satisfy naming heuristic. |

## Operational security: separate from the remaining Sonar findings

On 23 Nov 2026 the operator rotated the **actual** `membership` and
`membership_runtime` passwords on the staging VM. Both private
credentials accepted over PostgreSQL TCP; both former source-controlled
passwords rejected. The existing PG Docker data volume was preserved,
bot and supervisor restarted successfully; API stayed healthy; there
were 10 `membership_runtime` and 1 `membership` connections,
zero application error-string matches and zero container restarts.
A fresh encrypted off-VM backup on 23 Nov 2026 at 19:01 UTC
finished with `Result=success`, `ExecMainStatus=0`.
This is evidence of successful *upload*, not a second restore drill.

**Do not interpret the open CI-only test password findings as proof that
live credentials are still compromised.** The live password rotation is
already complete and was verified separately.

## Review and completion procedure

1. Run CI with generated per-run credentials; Alembic migrations, PostgreSQL
   RLS integration tests, backup contracts, setup tests and cold starts must
   all pass before merge. Sonar should be checked for new warnings at exact
   PR HEAD and for historical status after merge into main.
2. For six remaining scanner findings, evaluate the actual taint propagation
   and Cloud Run deployment evidence. If Sonar continues to flag safe code,
   record an issue-level `Accept`/false-positive disposition **with precise
   justification** and a reference to tests. Do not globally suppress the
   rule or remove safe guards to force a green counter.
3. Do not claim that a PR Quality Gate of zero *new* problems eliminates all
   historical `main` findings. Re-query `api/issues/search` after CI/main
   analysis and compare exact issue keys and statuses.
4. No runtime code, database SQL, Telegram UX, payment logic, backup scripts,
   credentials or server deployment should change in this cleanup PR.
