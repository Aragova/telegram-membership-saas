"""CI must not reuse source-controlled PostgreSQL passwords.

End-to-end PostgreSQL migrations and tenant RLS tests in GitHub Actions
verify the per-attempt credentials against a real disposable PG 17 service.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_role_passwords_are_unique_per_workflow_attempt() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    owner = "${{ format('ci-owner-{0}-{1}', github.run_id, github.run_attempt) }}"
    runtime = "${{ format('ci-runtime-{0}-{1}', github.run_id, github.run_attempt) }}"
    assert workflow.count(owner) >= 3  # job env, Compose, PG service
    assert workflow.count(runtime) >= 2  # job env, Compose
    assert "CI_PG_OWNER_PASSWORD: " + owner in workflow
    assert "CI_PG_RUNTIME_PASSWORD: " + runtime in workflow
    assert "POSTGRES_PASSWORD: " + owner in workflow

    assert "password=os.environ[\"CI_PG_OWNER_PASSWORD\"]" in workflow
    assert 'runtime_password = os.environ["CI_PG_RUNTIME_PASSWORD"]' in workflow
    assert "SELECT format('ALTER ROLE membership_runtime PASSWORD %L'" in workflow
    assert "await connection.execute(role_password_sql)" in workflow
    assert "CREATE ROLE membership_runtime LOGIN" in workflow

    # Both clients, the database service and the role init share one key pair.
    assert "membership_runtime:ci-runtime-{0}-{1}@localhost" in workflow
    assert "postgres:ci-owner-{0}-{1}@localhost" in workflow
    assert "github.run_attempt" in workflow
