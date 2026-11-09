"""Non-invasive tests for the future ViMi Pay SaaS billing calculator."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.platform_billing_policy import (
    PLATFORM_COMMISSION_RATE,
    PLATFORM_FREE_GRANT_MONTHLY_QUOTA,
    PLATFORM_SERVICE_FEE_RUB,
    PLATFORM_TRIAL_HOURS,
    ApprovedSaleSnapshot,
    calculate_commission_lines,
    total_commission_rub,
)

MONTH = date(2026, 11, 1)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _sale(
    kind="subscription",
    amount=Decimal("500.00"),
    currency="RUB",
    when=NOW,
    source_id=None,
):
    return ApprovedSaleSnapshot(
        source_kind=kind,
        source_id=source_id or uuid4(),
        merchant_id=uuid4(),
        approved_at=when,
        amount=amount,
        currency_code=currency,
    )


def _lines(*sales, rates=None, **kwargs):
    return calculate_commission_lines(
        sales,
        billing_month=MONTH,
        rub_per_unit=rates or {},
        **kwargs,
    )


def test_agreed_commercial_constants():
    assert PLATFORM_TRIAL_HOURS == 72
    assert PLATFORM_SERVICE_FEE_RUB == Decimal("150.00")
    assert PLATFORM_COMMISSION_RATE == Decimal("0.05")
    assert PLATFORM_FREE_GRANT_MONTHLY_QUOTA == 3


def test_approved_subscription_and_manual_file_fee_use_frozen_amounts():
    subscription = _sale()
    file = _sale(kind="file", amount=Decimal("1000.00"))
    lines = _lines(subscription, file)
    assert [line.commission_rub for line in lines] == [
        Decimal("25.00"), Decimal("50.00"),
    ]
    assert total_commission_rub(lines) == Decimal("75.00")
    assert lines[0].source_id == subscription.source_id
    assert lines[1].source_kind == "file"
    assert lines[0].original_amount == Decimal("500.00")


def test_explicit_fx_conversion_and_native_currency_snapshot():
    sale = _sale(kind="file", amount=Decimal("12.00"), currency="EUR")
    (line,) = _lines(sale, rates={"EUR": Decimal("85.00")})
    assert line.original_amount == Decimal("12.00")
    assert line.currency_code == "EUR"
    assert line.rub_per_unit == Decimal("85.00")
    assert line.gross_rub == Decimal("1020.00")
    assert line.commission_rub == Decimal("51.00")


def test_absent_fx_rate_does_not_turn_a_sale_into_zero_commission():
    with pytest.raises(ValueError, match="Missing configured RUB conversion rate"):
        _lines(_sale(currency="USD"))


def test_float_and_invalid_currency_rates_are_rejected():
    sale = _sale(currency="UAH")
    for rate in (0, -1, float("nan"), Decimal("NaN"), Decimal("0")):
        with pytest.raises(ValueError):
            _lines(sale, rates={"UAH": rate})


def test_duplicate_approval_source_cannot_be_billed_twice():
    sale = _sale()
    with pytest.raises(ValueError, match="Duplicate approved purchase source"):
        _lines(sale, sale)


def test_same_uuid_in_distinct_payment_sources_is_not_assumed_same_purchase():
    id_ = uuid4()
    subscription = _sale(source_id=id_)
    file = _sale(kind="file", source_id=id_)
    assert len(_lines(subscription, file)) == 2


def test_moscow_calendar_month_boundary_is_not_utc_midnight():
    # 21:00 UTC on Oct 31 is Nov 1 00:00 in Moscow.
    boundary = _sale(when=datetime(2026, 10, 31, 21, tzinfo=UTC))
    too_early = _sale(when=datetime(2026, 10, 31, 20, 59, tzinfo=UTC))
    (line,) = _lines(boundary, too_early)
    assert line.source_id == boundary.source_id


def test_trial_sales_before_billable_from_are_not_charged():
    trial_sale = _sale(when=datetime(2026, 11, 2, tzinfo=UTC))
    paid_period_sale = _sale(when=datetime(2026, 11, 5, tzinfo=UTC))
    (line,) = _lines(
        trial_sale, paid_period_sale,
        billable_from=datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert line.source_id == paid_period_sale.source_id


def test_nonfirst_billing_date_or_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="first"):
        calculate_commission_lines((), billing_month=date(2026, 11, 9), rub_per_unit={})
    with pytest.raises(ValueError, match="timezone"):
        _lines(_sale(when=datetime(2026, 11, 9)))
    with pytest.raises(ValueError, match="timezone"):
        _lines(_sale(), billable_from=datetime(2026, 11, 4))


def test_amount_must_be_positive_decimal():
    for amount in (Decimal("0"), Decimal("-1"), Decimal("NaN"), 500.0):
        with pytest.raises(ValueError, match="sale amount"):
            _lines(_sale(amount=amount))


def test_round_each_purchase_fee_before_sum():
    lines = _lines(_sale(amount=Decimal("0.10")), _sale(amount=Decimal("0.10")))
    assert [line.commission_rub for line in lines] == [
        Decimal("0.01"), Decimal("0.01"),
    ]
    assert total_commission_rub(lines) == Decimal("0.02")


def test_reject_unsupported_source_and_rate_without_modifying_inputs():
    sale = _sale()
    bad = replace(sale, source_kind="manual_grant")
    with pytest.raises(ValueError, match="Unknown"):
        _lines(bad)
    with pytest.raises(ValueError, match="commission_rate"):
        _lines(sale, commission_rate=Decimal("1.01"))
    assert sale.amount == Decimal("500.00")
