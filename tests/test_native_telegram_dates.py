"""Telegram 2026 native dates: HTML safety, fallback, RU/EN and timezones."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_expiry import (
    PaidDeadline,
    render_expiry_digest,
)
from telegram_membership_saas.modules.telegram_commerce.account import (
    BuyerAccount,
    BuyerSubscription,
    account_list_screen,
)
from telegram_membership_saas.telegram_dates import native_telegram_time


def test_local_fallback_preserves_utc_timestamp_and_moscow_offset():
    event = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)
    html = native_telegram_time(
        event, "Europe/Moscow", now=datetime(2026, 11, 9, tzinfo=UTC),
    )
    assert f'unix="{int(event.timestamp())}"' in html
    assert 'format="dt"' in html
    assert ">10.11.2026 00:30</tg-time>" in html
    assert "&lt;" not in html


def test_date_only_and_relative_formats_have_readable_fallback():
    event = datetime(2026, 12, 20, 10, tzinfo=UTC)
    now = datetime(2026, 11, 9, tzinfo=UTC)
    short = native_telegram_time(event, "Europe/Moscow", fmt="d", now=now)
    relative = native_telegram_time(event, "Europe/Moscow", fmt="r", now=now)
    assert 'format="d">20.12.2026</tg-time>' in short
    assert 'format="r">20.12.2026 13:00</tg-time>' in relative
    # Clients that cannot display relative dates still show an exact deadline.
    assert len("20.12.2026 13:00") < 31


def test_dst_uses_timezone_not_host_clock():
    event = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    html = native_telegram_time(
        event, "America/New_York",
        now=datetime(2026, 10, 30, tzinfo=UTC),
    )
    assert ">01.11.2026 01:30</tg-time>" in html


def test_unsupported_dates_and_invalid_formats_fail_safe():
    now = datetime(2026, 11, 9, tzinfo=UTC)
    far = now + timedelta(days=1099)
    before_epoch = datetime(1960, 1, 1, tzinfo=UTC)
    assert native_telegram_time(far, "Europe/Moscow", now=now) == "12.11.2029 03:00"
    assert "<tg-time" not in native_telegram_time(
        before_epoch, "Europe/Moscow", now=now,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        native_telegram_time(datetime(2026, 11, 9), "Europe/Moscow", now=now)
    with pytest.raises(ValueError, match="Unsupported"):
        native_telegram_time(now, "Europe/Moscow", fmt="dt\" bad=\"1", now=now)


@pytest.mark.parametrize("language", ["ru", "en"])
def test_buyer_account_has_native_subscription_deadline_and_lifetime(language):
    deadline = datetime(2026, 12, 9, 21, tzinfo=UTC)
    account = BuyerAccount(
        subscriptions_count=2,
        subscriptions=(
            BuyerSubscription(name="Gold & <VIP>", status="active", ends_at=deadline),
            BuyerSubscription(name="Forever", status="active", ends_at=None),
        ),
        timezone="Europe/Moscow",
    )
    token = _language.set(language)
    try:
        html, markup = account_list_screen(account, section="subs", page=0)
    finally:
        _language.reset(token)
    assert "Gold &amp; &lt;VIP&gt;" in html
    assert 'format="dt">10.12.2026 00:00</tg-time>' in html
    assert ("бессрочно" if language == "ru" else "lifetime") in html
    assert html.count("<tg-time") == 1
    assert markup.inline_keyboard[-1][0].callback_data == "account:catalog"


def test_expiry_digest_shows_native_time_but_does_not_change_group_logic():
    deadline = datetime(2026, 11, 10, 21, 15, tzinfo=UTC)
    row = PaidDeadline(
        customer_id=uuid4(), plan_id=uuid4(), ends_at=deadline,
        status="active", started_at=datetime(2026, 10, 1, tzinfo=UTC),
        username="buyer", first_name=None, plan_name="Plus & Gold",
    )
    text = render_expiry_digest(
        {7: [], 3: [], 1: [row], 0: []},
        local_date=datetime(2026, 11, 10, tzinfo=UTC).date(),
        timezone="Europe/Moscow",
    )
    assert text is not None
    assert 'format="dt">11.11.2026 00:15</tg-time>' in text
    assert "Plus &amp; Gold" in text
    assert "@buyer" in text
