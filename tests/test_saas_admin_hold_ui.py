"""No misleading sales-ready text when ViMi Pay applies a real commerce hold."""
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.handlers import (
    _sales_status_text,
)


def test_russian_dashboard_reports_hold_without_new_buttons():
    token = _language.set("ru")
    try:
        assert "приостановлены" in _sales_status_text(
            configured=True, hold_state="held",
        )
        assert "Продажи доступны" in _sales_status_text(
            configured=True, hold_state="clear",
        )
        assert "Не удалось проверить" in _sales_status_text(
            configured=True, hold_state=None,
        )
    finally:
        _language.reset(token)


def test_english_dashboard_reports_hold_without_new_buttons():
    token = _language.set("en")
    try:
        assert "on hold" in _sales_status_text(
            configured=True, hold_state="held",
        )
        assert "Sales available" in _sales_status_text(
            configured=True, hold_state="clear",
        )
        assert "could not be checked" in _sales_status_text(
            configured=True, hold_state=None,
        )
    finally:
        _language.reset(token)
