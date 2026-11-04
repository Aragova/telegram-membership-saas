"""Renewed purchases must never inherit cancelled paid periods."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telegram_membership_saas.modules.payments.approval_service import renewal_period_start


def test_cancelled_subscription_new_purchase_starts_immediately():
    now = datetime(2026, 11, 4, 10, tzinfo=UTC)
    previous = SimpleNamespace(
        status="cancelled", current_period_end=now + timedelta(days=20),
    )
    assert renewal_period_start(previous, now) == now


def test_active_subscription_renewal_extends_from_future_period_end():
    now = datetime(2026, 11, 4, 10, tzinfo=UTC)
    future_end = now + timedelta(days=20)
    previous = SimpleNamespace(status="active", current_period_end=future_end)
    assert renewal_period_start(previous, now) == future_end


def test_expired_period_or_missing_subscription_starts_today():
    now = datetime(2026, 11, 4, 10, tzinfo=UTC)
    previous = SimpleNamespace(
        status="active", current_period_end=now - timedelta(days=1),
    )
    assert renewal_period_start(previous, now) == now
    assert renewal_period_start(None, now) == now
