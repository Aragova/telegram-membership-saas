"""Safety checks for the independent, opt-in Stage-4 API deployment path."""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/api-staging-deploy.yml"
GATE = REPO / "ops/deploy/vimi-api-ssh-gate.sh.example"
ROOT = REPO / "ops/deploy/vimi-api-root-deploy.sh.example"


def test_api_only_workflow_does_not_reuse_bot_deploy() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in source
    assert "\n  push:" not in source
    assert "\n  pull_request:" not in source
    assert "github.ref == 'refs/heads/main'" in source
    assert "environment: api-staging" in source
    assert "vimi-api-deploy@" in source
    assert "PAY_API_STAGING_SSH_PRIVATE_KEY" in source
    assert "PAY_API_STAGING_SSH_KNOWN_HOSTS" in source
    assert "PAY_API_STAGING_SSH_HOST" in source
    assert "secrets.STAGING_SSH_PRIVATE_KEY" not in source
    assert "secrets.STAGING_SSH_KNOWN_HOSTS" not in source
    assert "vimi-deploy@" not in source
    assert "staging-deploy.yml" not in source
    assert "- connection-test" in source
    assert "- deploy" in source
    assert "cancel-in-progress: false" in source
    assert "StrictHostKeyChecking=yes" in source
    assert "API_DEPLOY_SSH_OK" in source


def test_forced_command_is_valid_shell_and_rejects_untrusted_input() -> None:
    subprocess.run(["bash", "-n", str(GATE)], check=True)
    subprocess.run(["bash", "-n", str(ROOT)], check=True)
    source = GATE.read_text(encoding="utf-8")
    assert "SSH_ORIGINAL_COMMAND" in source
    assert "sudo -n /usr/local/sbin/vimi-api-root-deploy" in source
    assert "docker" not in source
    assert "API_DEPLOY_SSH_OK" in source

    for original, status in (
        ("preflight", 0),
        ("", 2),
        ("deploy", 2),
        ("deploy " + "A" * 40, 2),
        ("deploy " + "a" * 40 + "; whoami", 2),
        ("deploy " + "a" * 39, 2),
        ("docker compose up -d", 2),
    ):
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=original)
        result = subprocess.run(
            ["bash", str(GATE)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == status, (original, result.stderr)
        if status == 0:
            assert result.stdout.strip() == "API_DEPLOY_SSH_OK"
        else:
            assert "DENIED" in result.stderr


def test_root_api_deploy_is_locked_to_current_main_and_loopback() -> None:
    source = ROOT.read_text(encoding="utf-8")
    assert "/opt/vimi-pay-api/repo" in source
    assert "id -u" in source
    assert "git status --porcelain" in source
    assert "refs/remotes/origin/main" in source
    assert '== "$requested"' in source
    assert "git merge-base --is-ancestor" in source
    assert "flock -w 180" in source
    assert "vimi-pay-public" in source
    assert "ops/public-api.compose.yaml" in source
    assert "config --format json" in source
    assert 'set(conf.get("services", {})) == {"pay-api"}' in source
    assert "127.0.0.1" in source
    assert '"18080"' in source
    assert "cap_drop" in source
    assert "read_only" in source
    assert "up -d --no-deps --build pay-api" in source
    assert "restore_on_error" in source
    assert "/health" in source
    assert "/payments" in source
    assert "PAY_API_DEPLOY_SUCCESS" in source
    assert "compose.light100.yaml" not in source
    assert "customer-supervisor" not in source
    assert "staging-deploy" not in source
    assert "systemctl reload nginx" not in source
    assert "iptables" not in source


def test_stage4_runbook_requires_dedicated_credentials_and_public_503() -> None:
    content = (REPO / "docs/STAGE4_PUBLIC_API.md").read_text(encoding="utf-8")
    assert "api-staging-deploy.yml" in content
    assert "PAY_API_STAGING_SSH_PRIVATE_KEY" in content
    assert "vimi-api-ssh-gate" in content
    assert "vimi-api-root-deploy" in content
    assert "no general sudo" in content.lower()
    assert "503" in content
    assert "no automatic deploy" in content.lower()
    assert "51" in content
