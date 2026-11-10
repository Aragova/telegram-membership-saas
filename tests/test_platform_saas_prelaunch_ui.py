"""Rich, bilingual and read-only ViMi Pay billing prelaunch information."""
import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _platform_bank_text,
)
from telegram_membership_saas.rich_messages import to_rich_html


@pytest.mark.parametrize(
    ("lang", "marker", "pending_label"),
    [
        ("ru", "Проверка собственных каналов", "Нужна проверка"),
        ("en", "Platform-owned channel verification", "Pending review"),
    ],
)
def test_bank_screen_shows_bilingual_rich_preflight_without_activation(
    lang, marker, pending_label,
):
    details = {
        "configured": True,
        "instructions": "TEST bank & <literal> account",
        "billing_enabled": False,
        "trial_enabled": False,
        "holds_enabled": False,
        "preflight": {
            "operator_bots": 2,
            "verified": 1,
            "pending": 1,
            "pending_bots": [
                {"username": "review_me_bot", "state": "not_exempt"},
            ],
        },
    }
    token = _language.set(lang)
    try:
        html = _platform_bank_text(details)
    finally:
        _language.reset(token)
    rich = to_rich_html(html)
    assert marker in html
    assert pending_label in html
    assert "review_me_bot" in html
    assert "TEST bank &amp; &lt;literal&gt; account" in html
    assert "<table bordered compact>" in rich
    assert rich.count("<table bordered compact>") >= 2
    assert "platform_saas_issue_bot_invoice" not in rich


def test_missing_preflight_is_rendered_without_claiming_ready():
    details = {
        "configured": False, "instructions": None,
        "billing_enabled": False, "trial_enabled": False,
        "holds_enabled": False,
    }
    html = _platform_bank_text(details)
    assert "ViMi Pay" in html
    assert "проверки" in html.lower() or "verification" in html.lower()
    assert "0 собственных" not in html


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_multiline_bank_instructions_are_never_tableized(lang):
    instructions = (
        "Bank transfer\n"
        "Account: 123456789\n"
        "Reference: invoice ID & <exact>"
    )
    details = {
        "configured": True, "instructions": instructions,
        "billing_enabled": False, "trial_enabled": False,
        "holds_enabled": False,
        "preflight": {
            "operator_bots": 1, "verified": 0, "pending": 1,
            "pending_bots": [{"username": "pending_bot", "state": "not_exempt"}],
        },
    }
    token = _language.set(lang)
    try:
        legacy = _platform_bank_text(details)
    finally:
        _language.reset(token)
    converted = to_rich_html(legacy)
    pre = converted.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert pre == (
        "Bank transfer\nAccount: 123456789\n"
        "Reference: invoice ID &amp; &lt;exact&gt;"
    )
    assert "<table" not in pre
    assert "<table bordered compact>" in converted
