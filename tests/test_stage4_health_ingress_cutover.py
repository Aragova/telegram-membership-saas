"""Regression guard for the optional Stage-4C health-only Nginx switch.

These tests check versioned sample files; they cannot prove VM configuration.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INGRESS = ROOT / "ops/ingress"


def test_health_proxy_is_an_exact_location_only_not_a_whole_vhost() -> None:
    snippet = (INGRESS / "pay-api.health-only.location.nginx.example").read_text(
        encoding="utf-8"
    )
    assert snippet.count("location = /health {") == 1
    assert snippet.count("proxy_pass http://127.0.0.1:18080/health;") == 1
    assert snippet.count("proxy_pass ") == 1
    assert "limit_except GET {" in snippet
    assert "deny all;" in snippet
    assert "proxy_set_header Host pay-api.usevimi.ru;" in snippet
    assert "proxy_connect_timeout 2s;" in snippet
    assert "proxy_read_timeout 5s;" in snippet
    assert "proxy_next_upstream off;" in snippet
    assert "location / {" not in snippet
    assert "\nserver {" not in snippet
    assert "listen " not in snippet
    assert "0.0.0.0:18080" not in snippet
    assert "api.usevimi.ru;" not in snippet
    assert "staging.usevimi.ru;" not in snippet
    assert "8000:8000" not in snippet


def test_stage3_rollback_still_returns_closed_503() -> None:
    baseline = (INGRESS / "pay-api.https.nginx.conf.example").read_text(
        encoding="utf-8"
    )
    assert "return 503" in baseline
    assert "api_not_deployed" in baseline
    assert "proxy_pass" not in baseline
    assert "location / {" in baseline
    assert "return 404;" in baseline


def test_smoke_script_is_read_only_https_only_and_checks_denials() -> None:
    path = INGRESS / "pay-api-health-smoke.sh"
    subprocess.run(["bash", "-n", str(path)], check=True)
    source = path.read_text(encoding="utf-8")
    assert 'base="https://pay-api.usevimi.ru"' in source
    assert "--proto '=https'" in source
    assert "--insecure" not in source
    assert "curl -k" not in source
    assert "docker " not in source
    assert "systemctl " not in source
    assert "nginx -s" not in source
    assert "STAGE4C HEALTH-ONLY INGRESS: PASS" in source
    for route in (
        "/payments", "/subscriptions", "/webhooks/telegram",
        "/docs", "/openapi.json", "/health/",
    ):
        assert route in source
