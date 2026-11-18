"""Short Lava checkout links: no new money operations, bounded public redirect."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.platform_saas_payment_redirect import (
    decode_lava_order,
    encode_lava_order,
    short_lava_url,
)
from telegram_membership_saas.public_api import app

REPO = Path(__file__).resolve().parents[1]


def test_lava_short_token_is_canonical_random_order_uuid() -> None:
    order = uuid4()
    token = encode_lava_order(order)
    assert len(token) == 22
    assert decode_lava_order(token) == order
    assert short_lava_url(order) == "https://pay-api.usevimi.ru/p/" + token
    assert "paymentParams=" not in short_lava_url(order)
    for bad in ("", "not-a-uuid", "a" * 21, "a" * 23, "." * 22, "a" * 21 + "="):
        assert decode_lava_order(bad) is None
    assert decode_lava_order("A" * 22) is None  # Not a canonical v4 UUID
    assert Settings(_env_file=None).saas_shortlinks_enabled is False


def test_public_api_stays_closed_without_dedicated_database(monkeypatch) -> None:
    monkeypatch.delenv("TMS_SHORTLINK_DATABASE_URL", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/p/" + encode_lava_order(uuid4())).status_code == 404
        assert client.get("/p/invalid").status_code == 404
        for path in ("/payments", "/webhooks/telegram", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("target,code", [
    ("https://app.lava.top/products/abc?paymentParams=SECRET&signature=SIGNED", 302),
    ("https://evil.example/checkout", 404),
    ("https://app.lava.top.evil.example/pay", 404),
    ("http://app.lava.top/pay", 404),
    (None, 404),
])
def test_redirect_only_allows_provider_https_and_never_exposes_url(
    monkeypatch, target, code,
) -> None:
    monkeypatch.delenv("TMS_SHORTLINK_DATABASE_URL", raising=False)
    order = uuid4()
    lookup = AsyncMock(return_value=target)
    app.state.shortlink_lookup = lookup
    try:
        with TestClient(app) as client:
            response = client.get("/p/" + encode_lava_order(order), follow_redirects=False)
            assert response.status_code == code
            assert response.headers["cache-control"].startswith("no-store")
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-robots-tag"] == "noindex, nofollow"
            if code == 302:
                assert response.headers["location"] == target
                assert response.text == ""
            else:
                assert "SECRET" not in response.text
                assert "SIGNED" not in response.text
                assert "location" not in response.headers
            lookup.assert_awaited_once_with(order)
            assert client.post("/p/" + encode_lava_order(order)).status_code == 405
    finally:
        del app.state.shortlink_lookup


def test_failed_lookup_is_generic_503_without_leaking_db_details(monkeypatch) -> None:
    monkeypatch.delenv("TMS_SHORTLINK_DATABASE_URL", raising=False)
    app.state.shortlink_lookup = AsyncMock(side_effect=RuntimeError("SECRET-DB-URL"))
    try:
        with TestClient(app) as client:
            response = client.get("/p/" + encode_lava_order(uuid4()))
            assert response.status_code == 503
            assert "SECRET-DB-URL" not in response.text
            assert response.headers["cache-control"].startswith("no-store")
    finally:
        del app.state.shortlink_lookup


def test_migration_has_read_only_isolated_role_and_no_new_invoice_table() -> None:
    sql = (REPO / "migrations" / "versions" /
           "0097_saas_payment_shortlinks.py").read_text(encoding="utf-8")
    assert "CREATE ROLE membership_shortlink_reader NOLOGIN" in sql
    assert "SECURITY DEFINER" in sql
    assert "REVOKE ALL ON FUNCTION public.platform_saas_lava_shortlink_target" in sql
    assert "FROM PUBLIC" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "platform_saas_v2_remaining" in sql
    assert "platform_saas_gateway_charges" in sql
    assert "CREATE TABLE" not in sql
    assert "INSERT INTO" not in sql
    assert "UPDATE public" not in sql


def test_public_api_deployment_not_reusing_bot_secrets_or_exposing_ports() -> None:
    compose = (REPO / "ops" / "public-api.compose.yaml").read_text(encoding="utf-8")
    assert "TMS_SHORTLINK_DATABASE_URL" in compose
    assert "TMS_SAAS_LAVA_TOP_API_KEY" not in compose
    assert "TMS_TELEGRAM_BOT_TOKEN" not in compose
    assert "127.0.0.1:18080:8000" in compose
    assert "--no-access-log" in compose
    assert "external: true" in compose
    assert "TMS_DATABASE_URL:" not in compose


def test_api_deploy_validates_canonical_network_mapping() -> None:
    """Execute the actual Python Compose safety gate on canonical Compose JSON."""
    source = (
        REPO / "ops/deploy/vimi-api-root-deploy.sh.example"
    ).read_text(encoding="utf-8")
    begin = source.index("import json, sys\nconf = json.load(sys.stdin)")
    end = source.index("\n'\ndocker compose", begin)
    validate = source[begin:end]
    expected = {
        "services": {
            "pay-api": {
                "ports": [{
                    "host_ip": "127.0.0.1",
                    "published": "18080",
                    "target": 8000,
                    "protocol": "tcp",
                }],
                "read_only": True,
                "user": "65534:65534",
                "cap_drop": ["ALL"],
                "environment": {
                    "TMS_ENVIRONMENT": "staging",
                    "TMS_SHORTLINK_DATABASE_URL": "",
                },
                "networks": {"staging-db": {}},
            }
        },
        "networks": {
            "staging-db": {
                "name": "telegram-membership-saas-staging_default",
                "external": True,
            },
        },
    }

    def check(compose: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", validate],
            input=json.dumps(compose), text=True,
            capture_output=True, check=False, timeout=5,
        )

    assert check(expected).returncode == 0
    if shutil.which("docker") and subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False,
        timeout=5,
    ).returncode == 0:
        # CI has Compose. Verify the exact real canonical JSON, not only
        # a plausible handcrafted example (a previous PR failed here).
        env = dict(
            os.environ,
            TMS_SHORTLINK_DATABASE_URL="",
            TMS_BOT_DOCKER_NETWORK="telegram-membership-saas-staging_default",
        )
        rendered = subprocess.run(
            [
                "docker", "compose", "-p", "vimi-pay-public",
                "-f", str(REPO / "ops/public-api.compose.yaml"),
                "config", "--format", "json",
            ],
            env=env, text=True, capture_output=True,
            check=True, timeout=15,
        )
        canonical = json.loads(rendered.stdout)
        assert set(canonical["services"]["pay-api"]["networks"]) == {"staging-db"}
        checked = check(canonical)
        assert checked.returncode == 0, checked.stderr
    wrong = json.loads(json.dumps(expected))
    wrong["services"]["pay-api"]["networks"] = ["staging-db"]
    assert check(wrong).returncode != 0
    wrong = json.loads(json.dumps(expected))
    wrong["services"]["pay-api"]["networks"] = {
        "staging-db": {}, "untrusted-network": {},
    }
    assert check(wrong).returncode != 0
    wrong = json.loads(json.dumps(expected))
    wrong["networks"]["staging-db"]["name"] = "wrong_network"
    assert check(wrong).returncode != 0
    wrong = json.loads(json.dumps(expected))
    wrong["services"]["pay-api"]["ports"][0]["host_ip"] = "0.0.0.0"
    assert check(wrong).returncode != 0


def test_shortlink_nginx_is_not_allowed_to_log_bearer_tokens() -> None:
    ingress = (
        REPO / "ops/ingress/pay-api.lava-shortlink.location.nginx.example"
    ).read_text(encoding="utf-8")
    assert 'location ~ "^/p/[A-Za-z0-9_-]{22}$"' in ingress
    assert "limit_except GET" in ingress
    assert "access_log off;" in ingress
    # /dev/null also intercepts inherited error_log 'warn' for this location.
    assert "error_log /dev/null crit;" in ingress
    assert "proxy_pass http://127.0.0.1:18080;" in ingress
