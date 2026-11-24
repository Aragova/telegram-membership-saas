"""Recovery retries must not expose exception payloads or swallow cancellation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from telegram_membership_saas.modules import stars_subscription_recovery as stars
from telegram_membership_saas.modules.crypto_pay import recovery as crypto
from telegram_membership_saas.safe_diagnostics import safe_failure_site


@pytest.mark.parametrize("provider", ["stars", "crypto"])
async def test_recovery_loop_redacts_failure_and_preserves_retry(monkeypatch, caplog, provider):
    project_id = uuid4()
    bot = SimpleNamespace(id=42)
    sensitive = "synthetic-private-provider-response"
    reconcile = AsyncMock(side_effect=[RuntimeError(sensitive), asyncio.CancelledError()])
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    if provider == "stars":
        monkeypatch.setattr(stars, "reconcile_subscription_star_ledger", reconcile)
        loop = stars.run_subscription_stars_ledger_loop(
            None, bot=bot, project_id=project_id,
        )
    else:
        monkeypatch.setattr(crypto, "reconcile_crypto_pay_once", reconcile)
        loop = crypto.run_crypto_pay_recovery_loop(
            None, bot=bot, project_id=project_id, encryption_key=SecretStr("test"),
        )
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert reconcile.await_count == 2
    assert sensitive not in caplog.text
    assert str(project_id) in caplog.text
    assert "RuntimeError" in caplog.text
    expected_site = (
        "run_subscription_stars_ledger_loop" if provider == "stars"
        else "run_crypto_pay_recovery_loop"
    )
    assert f":{expected_site}:" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_failure_site_without_application_frame():
    assert safe_failure_site(RuntimeError("private payload")) == "unknown"
    try:
        raise RuntimeError("private payload")
    except RuntimeError as exc:
        assert safe_failure_site(exc) == "unknown"
