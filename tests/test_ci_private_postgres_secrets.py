"""CI database credentials are cryptographically generated, never published.

PostgreSQL 17 migrations, RLS integration tests and bootstrap check that the
random credentials actually work on each CI run.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_uses_random_postgresql_passwords_and_loopback_only() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("secrets.token_urlsafe(36)") == 2
    assert 'echo "::add-mask::$owner_pw"' in workflow
    assert 'echo "::add-mask::$runtime_pw"' in workflow

    for key in (
        "CI_PG_OWNER_PASSWORD",
        "CI_PG_RUNTIME_PASSWORD",
        "VIMI_PG_OWNER_PASSWORD",
        "VIMI_PG_RUNTIME_PASSWORD",
    ):
        assert f"printf '{key}=%s" in workflow

    assert "postgresql+asyncpg://membership_runtime:%s@localhost" in workflow
    assert "postgresql+asyncpg://postgres:%s@localhost" in workflow
    assert 'POSTGRES_PASSWORD=%s' in workflow
    assert '>> "$GITHUB_ENV"' in workflow
    assert "--env-file" in workflow
    assert "--publish 127.0.0.1:5432:5432" in workflow
    assert "postgres:17-alpine" in workflow
    assert "if: always()" in workflow

    assert 'password=os.environ["CI_PG_OWNER_PASSWORD"]' in workflow
    assert 'runtime_password = os.environ["CI_PG_RUNTIME_PASSWORD"]' in workflow
    assert "SELECT format('ALTER ROLE membership_runtime PASSWORD %L'" in workflow
    assert "await connection.execute(role_password_sql)" in workflow
    assert "CREATE ROLE membership_runtime LOGIN" in workflow
