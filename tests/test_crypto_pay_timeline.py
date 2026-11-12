"""Cross-rail historical refund periods include earlier paid Crypto Pay time."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.stars_subscription_timeline import (
    _virtual_refunded_anchors,
    historical_stars_start,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
    SubscriptionStarRefund,
)


def test_virtual_refunded_stars_period_stays_anchored_to_prior_crypto_purchase():
    """Late recovery must not shorten a period bought before a Stars refund."""
    jan1 = datetime(2026, 1, 1, tzinfo=UTC)
    paid_2 = jan1 + timedelta(days=1)
    refunded_4 = jan1 + timedelta(days=3)
    crypto = CryptoPayInvoice(
        id=uuid4(), paid_at=jan1, created_at=jan1,
        kind="subscription", duration_value=30, duration_unit="day",
    )
    crypto_grant = EntitlementGrant(valid_until=jan1 + timedelta(days=30))
    star_order = SubscriptionStarOrder(
        id=uuid4(), paid_at=paid_2, created_at=paid_2,
        duration_value=30, duration_unit="day",
    )
    star_refund = SubscriptionStarRefund(
        order_id=star_order.id, refunded_at=refunded_4,
    )
    refunded = [(EntitlementGrant(), star_order, star_refund)]
    virtual = _virtual_refunded_anchors(
        [], [], refunded, (), "UTC", crypto=[(crypto_grant, crypto)],
    )
    assert len(virtual) == 1
    assert virtual[0][2] == jan1 + timedelta(days=60)
    # An unrelated purchase paid Jan 3 was valid BEFORE the Jan 4 refund;
    # its start must account for the whole historical Stars renewal.
    effective_start = historical_stars_start(
        jan1 + timedelta(days=2), [], [], refunded,
        (), "UTC", crypto=[(crypto_grant, crypto)],
    )
    assert effective_start == jan1 + timedelta(days=60)
