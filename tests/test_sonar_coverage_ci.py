"""Contracts for Sonar coverage integration, without any cloud token.

CI emits a real XML report before a scanner can upload anything.
Fork PRs must never receive the SonarQube analysis token.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_coverage_lock_is_hash_pinned_and_isolated() -> None:
    content = (ROOT / "requirements-coverage.lock").read_text()
    for name in ("coverage==7.13.3", "pytest-cov==7.0.0"):
        assert name in content
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", content)
    assert len(hashes) == 3 and len(set(hashes)) == 3

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert (
        "pip install --no-deps --require-hashes --only-binary=:all: "
        "-r requirements-coverage.lock"
    ) in workflow
    assert "python -m pip check" in workflow
    alembic_run = "python -m coverage run -m alembic upgrade head"
    pytest_run = "pytest --cov --cov-append --cov-branch --cov-report=xml:coverage.xml"
    assert alembic_run in workflow
    assert pytest_run in workflow
    assert workflow.index(alembic_run) < workflow.index(pytest_run)
    assert "0098_saas_stars_usd_quote.py" in workflow
    assert "ALEMBIC_COVERAGE_VALID=OK" in workflow
    assert "COVERAGE_XML_VALID=OK" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "retention-days: 14" in workflow


def test_sonar_scan_is_opt_in_and_excludes_untrusted_forks() -> None:
    content = (ROOT / ".github/workflows/ci.yml").read_text()
    assert content.count("vars.SONAR_CI_ENABLED == 'true'") == 2
    assert content.count("github.event.pull_request.head.repo.full_name == github.repository") == 2
    assert content.count("SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}") == 2
    assert "SonarSource/sonarqube-scan-action@70bad232163b09d998ea0707ee80c0ec80798c16" in content
    assert "fetch-depth: 0" in content
    assert "persist-credentials: false" in content


def test_sonar_properties_preserve_source_and_test_scopes() -> None:
    scanner = (ROOT / "sonar-project.properties").read_text()
    assert "sonar.projectKey=Aragova_telegram-membership-saas" in scanner
    assert "sonar.organization=aragova" in scanner
    assert not re.search(r"^sonar\.(sources|tests)\s*=", scanner, re.MULTILINE)
    assert "sonar.exclusions=tests/**/*" in scanner
    assert "sonar.python.coverage.reportPaths=coverage.xml" in scanner

    config = (ROOT / ".coveragerc").read_text()
    assert "branch = True" in config
    assert "relative_files = True" in config
    assert "include_namespace_packages = True" in config
    assert "src/telegram_membership_saas" in config
    assert "ops" in config and "migrations" in config
