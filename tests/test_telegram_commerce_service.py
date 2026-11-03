from decimal import Decimal
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.plans import DurationUnit
from telegram_membership_saas.modules.telegram_commerce import (
    decode_uuid,
    encode_uuid,
    format_amount,
    format_period,
)


def test_compact_uuid_roundtrip() -> None:
    value = uuid4()
    encoded = encode_uuid(value)

    assert len(encoded) == 22
    assert decode_uuid(encoded) == value


@pytest.mark.parametrize("value", ["", "x", "!" * 22, "a" * 21, "a" * 23])
def test_compact_uuid_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid compact UUID"):
        decode_uuid(value)


def test_format_amount_uses_currency_precision_without_trailing_zeroes() -> None:
    assert format_amount(Decimal("10.000000000000000000"), 2) == "10"
    assert format_amount(Decimal("10.250000000000000000"), 2) == "10.25"
    assert format_amount(Decimal("0.123456000000000000"), 6) == "0.123456"
    assert (
        format_amount(Decimal("99999999999999999999.123456789012345678"), 18)
        == "99999999999999999999.123456789012345678"
    )


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (1, DurationUnit.DAY.value, "1 день"),
        (2, DurationUnit.DAY.value, "2 дня"),
        (5, DurationUnit.DAY.value, "5 дней"),
        (11, DurationUnit.MONTH.value, "11 месяцев"),
        (21, DurationUnit.MONTH.value, "21 месяц"),
        (1, DurationUnit.YEAR.value, "1 год"),
        (None, DurationUnit.LIFETIME.value, "навсегда"),
    ],
)
def test_format_period(value: int | None, unit: str, expected: str) -> None:
    assert format_period(value, unit) == expected
