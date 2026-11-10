"""Bank details stay short; paginated exemption audit is Rich and bilingual."""
import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _platform_bank_text,
    _platform_exemption_text,
)
from telegram_membership_saas.rich_messages import to_rich_html


@pytest.mark.parametrize(
    ("lang", "title", "pending"),
    [
        ("ru", "Исключения", "Нет подтверждения"),
        ("en", "Exemptions", "Unverified"),
    ],
)
def test_separate_exemption_audit_is_paginated_and_native_rich(
    lang, title, pending,
):
    snapshot = {
        "operator_bots": 25, "verified": 0, "pending": 25,
        "page": 1, "pages": 4,
        "pending_bots": [
            {"username": "bot_a", "state": "not_exempt"},
            {"username": "bot_b", "state": "needs_review"},
        ],
    }
    token = _language.set(lang)
    try:
        legacy = _platform_exemption_text(snapshot)
    finally:
        _language.reset(token)
    rendered = to_rich_html(legacy)
    assert title in rendered
    assert pending in legacy
    assert "<table bordered compact>" in rendered
    assert "@bot_a" in rendered and "@bot_b" in rendered
    assert ("Страница 2 из 4" if lang == "ru" else "Page 2 of 4") in rendered
    assert "Показаны первые 8" not in rendered
    assert "First 8 shown" not in rendered
    assert "25" in rendered
    assert "Собственных каналов: 25" not in rendered


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_bank_details_remain_short_and_preserve_multiline_content(lang):
    instructions = (
        "Bank transfer\n"
        "Account: 123456789\n"
        "Reference: invoice ID & <exact>"
    )
    details = {
        "configured": True, "instructions": instructions,
        "billing_enabled": False, "trial_enabled": False,
        "holds_enabled": False, "preflight": {
            "operator_bots": 25, "verified": 0, "pending": 25,
            "pending_bots": [{"username": "irrelevant_bot"}],
        },
    }
    token = _language.set(lang)
    try:
        html = _platform_bank_text(details)
    finally:
        _language.reset(token)
    converted = to_rich_html(html)
    pre = converted.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert pre == (
        "Bank transfer\nAccount: 123456789\n"
        "Reference: invoice ID &amp; &lt;exact&gt;"
    )
    assert "<table" not in pre
    assert "irrelevant_bot" not in html
    assert "25" not in html
    assert "Trial:" not in html
    assert "Тестовый период:" not in html


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_missing_bank_details_do_not_claim_billing_enabled(lang):
    details = {
        "configured": False, "instructions": None,
        "billing_enabled": False, "trial_enabled": False,
        "holds_enabled": False,
    }
    token = _language.set(lang)
    try:
        html = _platform_bank_text(details)
    finally:
        _language.reset(token)
    assert "ViMi Pay" in html
    assert ("не запускает биллинг" if lang == "ru" else
            "does not enable billing") in html
    assert "<pre>" not in html
