from datetime import UTC, datetime

import pytest

from telegram_membership_saas.modules.plans import DurationUnit
from telegram_membership_saas.modules.subscriptions import calculate_period_end


def test_calendar_month_is_not_fixed_thirty_days() -> None:
    start = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    end = calculate_period_end(
        start,
        timezone_name="Europe/Moscow",
        duration_value=1,
        duration_unit=DurationUnit.MONTH.value,
    )
    assert end == datetime(2026, 2, 28, 12, 0, tzinfo=UTC)


def test_calendar_year_clips_leap_day() -> None:
    start = datetime(2028, 2, 29, 9, 0, tzinfo=UTC)
    end = calculate_period_end(
        start,
        timezone_name="Europe/Moscow",
        duration_value=1,
        duration_unit=DurationUnit.YEAR.value,
    )
    assert end == datetime(2029, 2, 28, 9, 0, tzinfo=UTC)


def test_lifetime_has_no_period_end() -> None:
    start = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert (
        calculate_period_end(
            start,
            timezone_name="Europe/Moscow",
            duration_value=None,
            duration_unit=DurationUnit.LIFETIME.value,
        )
        is None
    )


def test_period_math_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_period_end(
            datetime(2026, 11, 3, 12, 0),
            timezone_name="Europe/Moscow",
            duration_value=1,
            duration_unit=DurationUnit.MONTH.value,
        )
