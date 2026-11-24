# SonarQube Cloud: CI coverage migration for ViMi Pay

## What the PR does now

The standard GitHub `CI` workflow continues to test the full project
against a disposable PostgreSQL 17 service, including Alembic upgrades,
the restricted runtime database role and RLS tests. The existing
`Test` step runs exactly once under `pytest-cov`, generating
`coverage.xml` with branch coverage, and uploads that report as a
short-lived GitHub Actions artifact. The workflow validates that
the XML is a nonempty coverage document before upload.

Measurement scope is explicit: `src/telegram_membership_saas/`, `ops/`,
and `migrations/`, including unimported Python files as uncovered.
Production containers do not install the coverage-only lock.
No numeric coverage percentage threshold is added before a baseline
can be measured and reviewed.

Coverage tools are pinned to release wheel SHA-256 in
`requirements-coverage.lock`, installed only in CI with
`--no-deps --require-hashes --only-binary=:all:` **after**
the existing hash-verified `requirements-dev.lock`. CI also runs
`pip check` to verify that pytest and pluggy dependencies exist.

## Why a manual Sonar mode change is unavoidable

**SonarQube Cloud Automatic Analysis cannot ingest external
coverage reports.** Simply adding `sonar.python.coverage.reportPaths`
to `.sonarcloud.properties` is not enough.

The PR provides a *disabled-by-default* Sonar scan step on GitHub
Actions. It runs only when repository variable
`SONAR_CI_ENABLED` is exactly `true` **and** the event is a
`push` to `main` or a same-repository PR. It never supplies
`SONAR_TOKEN` to fork PRs. Missing token causes the enabled scan
to fail instead of silently reporting success without coverage.

On a successful scanner run, `sonar-project.properties`
controls the full-repository scan and imports the newly produced
`coverage.xml`. The scan action is pinned to official
SonarSource `sonarqube-scan-action` v8.5.1 by commit SHA,
rather than a floating tag.

## One-time activation after a verified PR merge

1. In SonarQube Cloud → ViMi Pay project → **Administration →
   Analysis Method**, switch **Automatic Analysis OFF**. Stop using
   its automatic reports; do not run both modes simultaneously.
2. In SonarQube Cloud, create a token with **Execute Analysis**
   permission for the project. In GitHub repository →
   **Settings → Secrets and variables → Actions → Secrets**,
   create `SONAR_TOKEN` with that value. Never post the token
   to chat, issues or logs.
3. In the same GitHub Settings section → **Variables**, create
   `SONAR_CI_ENABLED` = `true`.
4. Re-run the existing CI workflow on `main` (or push an ordinary
   non-functional follow-up commit). Confirm scanner logs mention
   importing `coverage.xml` and SonarQube dashboard **Coverage**
   becomes numeric. Inspect scanner scope and warnings; if GitHub
   scan fails, revert `SONAR_CI_ENABLED=false` before investigation,
   and do **not** invent a percentage.

Until steps 1–4, CI still produces downloadable reports, but the
SonarQube dashboard will **continue to show no coverage**.

## Safety and limitations

- Does not change bot menus, payments, customer subscriptions,
  PostgreSQL schema or credentials, backups, runtime dependencies,
  Docker images, or deployed services.
- Coverage records execution by automated tests, not production
  correctness. A low percentage on initial baseline is useful
  information rather than an outage or reason to suppress paths.
- GitHub Actions artifacts expire after 14 days; the Sonar dashboard
  stores measures following a successful CI-based scanner analysis.
- The disposable PostgreSQL Alembic upgrade is measured by
  `python -m coverage run -m alembic upgrade head`. The existing test
  suite then appends its branch coverage with `pytest --cov-append`,
  rather than overwriting the migration execution data. CI asserts
  that the XML contains executed lines from revision 0098 to prevent
  silently returning to a misleading zero for migrations.
- Coverage of subprocesses started by tests may still be incomplete.
  This adjustment records real CI integration work; it does not
  replace targeted tests for payment, billing, and database security.
- No deploy or restart of the ViMi Pay VM is required.

## Cloud analysis scope warnings

The current Cloud analysis-scope UI ignores the legacy `sonar.sources`
and `sonar.tests` properties. Keep the existing exclusions and coverage
import unchanged; check the effective source/test classification in the
project's Analysis Scope UI and confirm that source, ops and migration
files still appear in the scanner report after this change.

The Oracle PL/SQL Data Dictionary warning is not a PostgreSQL connection
failure. ViMi Pay uses PostgreSQL, not Oracle. Do not provide production
credentials or turn off the SQL analyzer merely to remove this warning.
PostgreSQL migration execution, RLS tests and database security checks
remain mandatory.
