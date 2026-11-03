import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram_membership_saas.modules.plans import DurationUnit


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Subscription period timestamps must be timezone-aware")


def _add_months(local_moment: datetime, months: int) -> datetime:
    month_index = (local_moment.year * 12 + local_moment.month - 1) + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(local_moment.day, calendar.monthrange(year, month)[1])
    return local_moment.replace(year=year, month=month, day=day)


def calculate_period_end(
    start_at: datetime,
    *,
    timezone_name: str,
    duration_value: int | None,
    duration_unit: str,
) -> datetime | None:
    _require_aware(start_at)

    unit = DurationUnit(duration_unit)
    if unit is DurationUnit.LIFETIME:
        if duration_value is not None:
            raise ValueError("Lifetime duration must not have duration_value")
        return None

    if duration_value is None or duration_value <= 0:
        raise ValueError("Finite duration requires a positive duration_value")

    local_start = start_at.astimezone(ZoneInfo(timezone_name))
    if unit is DurationUnit.DAY:
        local_end = local_start + timedelta(days=duration_value)
    elif unit is DurationUnit.WEEK:
        local_end = local_start + timedelta(weeks=duration_value)
    elif unit is DurationUnit.MONTH:
        local_end = _add_months(local_start, duration_value)
    elif unit is DurationUnit.YEAR:
        local_end = _add_months(local_start, duration_value * 12)
    else:
        raise ValueError(f"Unsupported duration unit: {duration_unit}")

    return local_end.astimezone(UTC)
