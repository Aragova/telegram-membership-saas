"""Dashboard commercial state must match real billing and checkout rules."""
from datetime import UTC, datetime, timedelta

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.handlers import (
    _paid_service_status_text,
    _sales_status_text,
)


def test_russian_dashboard_reports_hold_without_new_buttons():
    token = _language.set("ru")
    try:
        held = _sales_status_text(configured=True, hold_state="held")
        assert "приостановлены" in held
        assert "0 ₽" in held
        assert "ещё не" in held
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
        held = _sales_status_text(configured=True, hold_state="held")
        assert "on hold" in held
        assert "0 due" in held
        assert "not active" in held
        assert "Sales available" in _sales_status_text(
            configured=True, hold_state="clear",
        )
        assert "could not be checked" in _sales_status_text(
            configured=True, hold_state=None,
        )
    finally:
        _language.reset(token)


def _settled_service_bill() -> dict:
    return {
        "invoice_id": "issued-verified-invoice",
        "total_rub": "150.00",
        "paid_rub": "150.00",
        "credit_rub": "0.00",
        "total_due_rub": "0.00",
        "period_start": "2026-11-16T14:53:00+00:00",
        "period_end": "2026-12-16T14:53:00+00:00",
    }


def test_paid_service_overrides_expired_trial_only_for_live_settled_invoice():
    now = datetime(2026, 11, 16, 16, 24, tzinfo=UTC)
    token = _language.set("ru")
    try:
        result = _paid_service_status_text(
            _settled_service_bill(), timezone="Europe/Moscow", now=now,
        )
        assert result == "✅ Обслуживание оплачено до 16.12.2026"
        assert "Пробный период" not in result

        debt = _settled_service_bill()
        debt["total_due_rub"] = "150.00"
        assert _paid_service_status_text(
            debt, timezone="Europe/Moscow", now=now,
        ) is None

        partial = _settled_service_bill()
        partial["paid_rub"] = "149.00"
        assert _paid_service_status_text(
            partial, timezone="Europe/Moscow", now=now,
        ) is None

        no_invoice = _settled_service_bill()
        no_invoice["invoice_id"] = None
        assert _paid_service_status_text(
            no_invoice, timezone="Europe/Moscow", now=now,
        ) is None

        # A historical settled invoice does not mean service is active now.
        assert _paid_service_status_text(
            _settled_service_bill(), timezone="Europe/Moscow",
            now=now + timedelta(days=32),
        ) is None
        assert _paid_service_status_text(
            _settled_service_bill(), timezone="Europe/Moscow",
            now=now - timedelta(days=2),
        ) is None
    finally:
        _language.reset(token)


def test_english_paid_service_label():
    token = _language.set("en")
    try:
        assert _paid_service_status_text(
            _settled_service_bill(), timezone="Europe/Moscow",
            now=datetime(2026, 11, 16, 16, 24, tzinfo=UTC),
        ) == "✅ Service paid until 16.12.2026"
    finally:
        _language.reset(token)
