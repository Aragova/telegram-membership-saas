"""Simulate the read-only PostgreSQL audit without touching a real server."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "ops/audit/vimi_postgres_readonly_audit.sh"


@pytest.mark.parametrize(
    ("invalid_login", "public_login", "expected"),
    [
        ("rejected", "accepted", "PUBLIC_PASSWORD_CONFIRMED"),
        ("accepted", "accepted", "ACCEPTED_UNVERIFIED_AUTH"),
        ("rejected", "rejected", "PUBLIC_PASSWORD_NOT_ACCEPTED"),
        ("unavailable", "accepted", "UNAVAILABLE"),
    ],
)
def test_audit_identifies_password_auth_without_false_assurances(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_login: str,
    public_login: str,
    expected: str,
) -> None:
    shell = AUDIT.read_text(encoding="utf-8")
    code = shell.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    info = {
        "Id": "c" * 64,
        "Name": "/vimi-postgres",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "telegram-membership-saas-staging",
                "com.docker.compose.service": "postgres",
            },
            "Env": [
                "POSTGRES_USER=membership",
                "POSTGRES_DB=membership",
                "POSTGRES_PASSWORD=membership",
            ],
        },
    }

    def response(out: str = "", rc: int = 0, error: str = ""):
        return SimpleNamespace(returncode=rc, stdout=out, stderr=error)

    def fake_run(argv, *, capture_output, text, timeout, check):
        if argv[:3] == ["docker", "ps", "-q"]:
            return response("container-id\n")
        if argv[:2] == ["docker", "inspect"]:
            return response(json.dumps([info]))
        if "pg_roles" in argv[-1]:
            return response(
                "DB_QUERY=OK\nROLE_membership_LOGIN=true_SUPERUSER=true\n"
                "HOST_AUTH_METHODS=scram-sha-256,trust\n"
            )
        if "-h" in argv and "-e" in argv:
            candidate = argv[argv.index("-e") + 1].split("=", 1)[1]
            role = argv[argv.index("-U") + 1]
            mode = (
                invalid_login
                if candidate.startswith("__vimi_invalid_password")
                else public_login
                if role == "membership"
                else "rejected"
            )
            if mode == "accepted":
                return response("1\n")
            if mode == "unavailable":
                raise subprocess.TimeoutExpired(argv, timeout)
            return response(rc=2, error="FATAL: password authentication failed")
        raise AssertionError("unexpected Docker diagnostic command")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec(compile(code, str(AUDIT), "exec"), {"__name__": "__main__"})
    out = capsys.readouterr().out
    assert "PUBLIC_PASSWORD_LOGIN_membership=" + expected in out
    assert "AUDIT_FINISHED=READ_ONLY" in out
    assert "PGPASSWORD=" not in out
