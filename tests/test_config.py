from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from telegram_membership_saas.config import Settings


def test_default_project_timezone_is_moscow() -> None:
    settings = Settings(_env_file=None)
    assert settings.default_project_timezone == "Europe/Moscow"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(default_project_timezone="Mars/Olympus", _env_file=None)


def test_unset_provider_credentials_from_compose_are_truly_disabled(monkeypatch) -> None:
    monkeypatch.setenv("TMS_SAAS_LAVA_TOP_API_KEY", "")
    monkeypatch.setenv("TMS_SAAS_LAVA_TOP_OFFER_ID", "")
    monkeypatch.setenv("TMS_SAAS_CRYPTO_PAY_API_TOKEN", "")
    monkeypatch.setenv("TMS_SAAS_CRYPTO_PAY_TESTNET", "false")
    cfg = Settings(_env_file=None)
    assert cfg.saas_lava_top_api_key is None
    assert cfg.saas_lava_top_offer_id is None
    assert cfg.saas_crypto_pay_api_token is None
    assert cfg.saas_crypto_pay_testnet is False


def test_configured_platform_credentials_are_read_by_central_bot(monkeypatch) -> None:
    offer = uuid4()
    monkeypatch.setenv("TMS_SAAS_LAVA_TOP_API_KEY", "secret-lava-for-test")
    monkeypatch.setenv("TMS_SAAS_LAVA_TOP_OFFER_ID", str(offer))
    monkeypatch.setenv("TMS_SAAS_CRYPTO_PAY_API_TOKEN", "secret-crypto-for-test")
    monkeypatch.setenv("TMS_SAAS_CRYPTO_PAY_TESTNET", "true")
    cfg = Settings(_env_file=None)
    assert cfg.saas_lava_top_api_key.get_secret_value() == "secret-lava-for-test"
    assert cfg.saas_lava_top_offer_id == offer
    assert cfg.saas_crypto_pay_api_token.get_secret_value() == "secret-crypto-for-test"
    assert cfg.saas_crypto_pay_testnet is True


def test_compose_only_forwards_platform_provider_secrets_to_central_bot() -> None:
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
    bot_part = compose.split("\n  bot:\n", 1)[1].split(
        "\n  customer-supervisor:\n", 1,
    )[0]
    tenant_part = compose.split("\n  customer-supervisor:\n", 1)[1]
    keys = (
        "TMS_SAAS_LAVA_TOP_API_KEY",
        "TMS_SAAS_LAVA_TOP_OFFER_ID",
        "TMS_SAAS_CRYPTO_PAY_API_TOKEN",
        "TMS_SAAS_CRYPTO_PAY_TESTNET",
    )
    for key in keys:
        assert f"{key}: ${{{key}:-" in bot_part
        assert key not in tenant_part
