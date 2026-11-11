"""Tenant-scoped gross merchant sales from independently confirmed payment rails.

No synthetic Payment is created for native Telegram Stars charges. Amounts
remain in original denominations (XTR is NOT fiat or converted USD).
Returned/refunded sales remain historical gross receipts; refund credits
are accounted for separately in the owner report.
"""
from uuid import UUID

from sqlalchemy import Numeric, cast, select, union_all

from telegram_membership_saas.modules.payments import Payment
from telegram_membership_saas.modules.stars_subscriptions import SubscriptionStarOrder


def merchant_confirmed_sales(project_id: UUID):
    manual = select(
        Payment.customer_id.label("customer_id"),
        Payment.plan_id.label("plan_id"),
        Payment.currency_code.label("currency_code"),
        Payment.final_amount.label("amount"),
        Payment.approved_at.label("paid_at"),
    ).where(Payment.project_id == project_id)
    stars = select(
        SubscriptionStarOrder.customer_id.label("customer_id"),
        SubscriptionStarOrder.plan_id.label("plan_id"),
        SubscriptionStarOrder.currency_code.label("currency_code"),
        cast(SubscriptionStarOrder.stars_amount, Numeric(38, 18)).label("amount"),
        SubscriptionStarOrder.paid_at.label("paid_at"),
    ).where(
        SubscriptionStarOrder.project_id == project_id,
        SubscriptionStarOrder.status == "paid",
    )
    return union_all(manual, stars).subquery("confirmed_merchant_sales")
