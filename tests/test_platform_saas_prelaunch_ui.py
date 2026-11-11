"""Bank details stay short, secure and bilingual."""
import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _platform_bank_text,
)
from telegram_membership_saas.rich_messages import to_rich_html


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
