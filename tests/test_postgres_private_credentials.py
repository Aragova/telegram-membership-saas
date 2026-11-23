"""Regression contracts: no public PostgreSQL password in Docker runtime.

The first-boot PostgreSQL hook is integration-tested in GitHub CI using a
disposable postgres:16-alpine container and two fresh random passwords.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_requires_private_passwords_for_every_runtime() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD: ${VIMI_PG_OWNER_PASSWORD:?" in compose
    assert "VIMI_PG_RUNTIME_PASSWORD: ${VIMI_PG_RUNTIME_PASSWORD:?" in compose
    assert "POSTGRES_PASSWORD: membership" not in compose

    runtime = (
        "postgresql+asyncpg://membership_runtime:"
        "${VIMI_PG_RUNTIME_PASSWORD:?Set private runtime password}"
        "@postgres:5432/membership"
    )
    owner = (
        "postgresql+asyncpg://membership:"
        "${VIMI_PG_OWNER_PASSWORD:?Set private owner password}"
        "@postgres:5432/membership"
    )
    assert compose.count(f"TMS_DATABASE_URL: {runtime}") == 4
    assert compose.count(f"TMS_MIGRATION_DATABASE_URL: {owner}") == 2
    assert "membership_runtime:membership_runtime@" not in compose
    assert "membership:membership@" not in compose
    assert "./docker/postgres/20-runtime-password.sh:" in compose
    # Do not change service topology or give the database a published port.
    postgres_service = compose.split("\n  postgres:\n", 1)[1].split(
        "\n  app:\n", 1
    )[0]
    assert "\n    ports:" not in postgres_service


def test_new_database_role_password_is_bootstrapped_without_literal() -> None:
    sql = (ROOT / "docker/postgres/init-runtime.sql").read_text(
        encoding="utf-8"
    )
    hook = ROOT / "docker/postgres/20-runtime-password.sh"
    shell = hook.read_text(encoding="utf-8")
    subprocess.run(["sh", "-n", str(hook)], check=True)
    assert "CREATE ROLE membership_runtime" in sql
    assert "NOSUPERUSER" in sql and "NOBYPASSRLS" in sql
    assert "PASSWORD 'membership_runtime'" not in sql
    assert 'VIMI_PG_RUNTIME_PASSWORD' in shell
    assert "ALTER ROLE membership_runtime" in shell
    assert "psql -X -q -v ON_ERROR_STOP=1" in shell
    # Authenticated role password is transmitted via stdin, not -c or -v args.
    assert '| psql -X -q' in shell


def test_no_passwords_are_supplied_as_production_examples() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "VIMI_PG_OWNER_PASSWORD=" in template
    assert "VIMI_PG_RUNTIME_PASSWORD=" in template
    assert "membership_runtime:membership_runtime@" not in template
    assert "membership:membership@" not in template
    runbook = (ROOT / "docs/POSTGRES_CREDENTIAL_ROTATION.md").read_text(
        encoding="utf-8"
    )
    assert "ALTER ROLE" in runbook
    assert "The existing PostgreSQL container" in runbook
    assert "does not rotate" in runbook.lower()
