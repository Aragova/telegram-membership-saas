"""PR #61: opt-in, accurate daily finance summaries for connected owners."""
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_daily import (
    DailyFinancialSnapshot,
    prepare_owner_daily_financial,
    previous_local_day_window,
    render_daily_financial_summary,
    schedule_owner_daily_financial,
)
from telegram_membership_saas.modules.owner_notifications import (
    OwnerNotificationDelivery,
    deliver_one_owner_notification,
    set_owner_setting,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    publish_owner_plan,
    save_owner_price,
)
from telegram_membership_saas.modules.payments import Payment
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantContext


def _tid():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


@pytest.mark.parametrize(
    ("local_date", "expected_hours"),
    [
        (date(2026, 3, 29), 23),
        (date(2026, 10, 25), 25),
        (date(2026, 6, 4), 24),
    ],
)
def test_local_previous_day_handles_daylight_saving(local_date, expected_hours):
    start, end = previous_local_day_window(local_date, "Europe/Amsterdam")
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC
    assert end - start == timedelta(hours=expected_hours)


@pytest.mark.parametrize(
    ("language", "title", "waiting"),
    [
        ("ru", "Ежедневный отчёт", "Заявок на проверке"),
        ("en", "Daily financial summary", "Awaiting review"),
    ],
)
def test_daily_summary_render_is_bilingual_safe_and_multi_currency(
    language, title, waiting,
):
    report = DailyFinancialSnapshot(
        project_name="Channel <VIP & GOLD>",
        timezone="Europe/Moscow",
        report_date=date(2026, 11, 5),
        approved_count=3,
        approved_totals=(
            ("RUB", Decimal("1550.000000000000000000"), 2),
            ("USD", Decimal("9.50"), 1),
        ),
        new_customers=4, active_paid_customers=9, waiting_review=5,
        as_of=datetime(2026, 11, 6, 7, 0, tzinfo=UTC),
    )
    token = _language.set(language)
    try:
        text = render_daily_financial_summary(report)
        assert title in text
        assert waiting in text
        assert "1550 RUB" in text
        assert "9.5 USD" in text
        assert "<VIP & GOLD>" not in text
        assert "Channel &lt;VIP &amp; GOLD&gt;" in text
        assert "05.11.2026" in text
        assert "3" in text and "5" in text
        assert "bank" in text or "банке" in text
        assert len(text) < 4096
    finally:
        _language.reset(token)


def test_zero_sales_do_not_claim_funds_or_create_fake_currency():
    report = DailyFinancialSnapshot(
        project_name="Quiet project",
        timezone="Europe/Moscow",
        report_date=date(2026, 11, 5), approved_count=0,
        approved_totals=(), new_customers=0,
        active_paid_customers=0, waiting_review=1,
        as_of=datetime(2026, 11, 6, 7, tzinfo=UTC),
    )
    token = _language.set("ru")
    try:
        text = render_daily_financial_summary(report)
        assert "Оплат подтверждено: 0" in text
        assert "Подтверждённых оплат нет" in text
        assert "Заявок на проверке: 1" in text
        assert "RUB" not in text
    finally:
        _language.reset(token)


@pytest.mark.skipif(not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL")
async def test_daily_opt_in_financial_facts_idempotency_mute_and_owner():
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    zone = ZoneInfo("Europe/Moscow")
    report_day = datetime.now(UTC).astimezone(zone).date()
    delivery_day = report_day + timedelta(days=1)
    due = datetime.combine(delivery_day, time(10), tzinfo=zone).astimezone(UTC)
    owner_tid, paid_tid, pending_tid = _tid(), _tid(), _tid()
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tid,
            username="daily_owner", first_name="Owner",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Daily revenue <test>",
        )
        other_owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="daily_other",
            first_name="Other",
        )
        unrelated = await create_owner_project(
            factory, actor_id=other_owner, name="Unrelated project",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert not (await __import__(
                "telegram_membership_saas.modules.owner_notifications",
                fromlist=["read_owner_settings"],
            ).read_owner_settings(session, project))["daily"]
            await set_owner_setting(
                session, project_id=project, actor_id=owner,
                category="invoice", enabled=False,
            )
            resource = Resource(
                project_id=project, resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Finance test channel",
            )
            session.add(resource)
            await session.flush()
            resource_id = resource.id

        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project, name="Premium",
            duration_value=1, duration_unit="month", resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="RUB", amount="550.00",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project, name="Manual bank",
            currency_code="RUB", instruction="Pay {amount} {currency}",
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )

        # An unpaid and even submitted invoice must not count as a sale.
        for customer_tid, approve in ((paid_tid, True), (pending_tid, False)):
            async with system_project_session_scope(factory, project) as session:
                option = (await list_checkout_options(
                    session, project_id=project,
                ))[0]
                request = await create_checkout_for_telegram_user(
                    session, project_id=project,
                    telegram_user_id=customer_tid,
                    username="paid" if approve else "pending",
                    first_name="Buyer", plan_price_id=option.plan_price_id,
                    payment_method_id=option.payment_method_id,
                )
            async with system_project_session_scope(factory, project) as session:
                await submit_checkout_for_telegram_user(
                    session, project_id=project,
                    request_id=request.request.id,
                    telegram_user_id=customer_tid,
                )
                if approve:
                    result = await review_payment_request_from_telegram(
                        session, project_id=project,
                        request_id=request.request.id,
                        actor_telegram_user_id=owner_tid, approve=True,
                    )
                    assert result.changed

        # The confirmed payment occurred on the full previous local day.
        previous_noon = datetime.combine(
            report_day, time(12), tzinfo=zone,
        ).astimezone(UTC)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            payment = await session.scalar(
                select(Payment).where(Payment.project_id == project)
            )
            assert payment is not None
            payment.approved_at = previous_noon

        next_due = await schedule_owner_daily_financial(
            factory, project_id=project, now=due - timedelta(minutes=1),
        )
        assert next_due == due
        assert await schedule_owner_daily_financial(
            factory, project_id=project, now=due + timedelta(minutes=1),
        ) > due
        async with system_project_session_scope(factory, project) as session:
            assert not (await session.scalars(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project,
                    OwnerNotificationDelivery.kind == "daily",
                )
            )).all()

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            await set_owner_setting(
                session, project_id=project, actor_id=owner,
                category="daily", enabled=True,
            )
        for offset in (2, 3, 4):
            await schedule_owner_daily_financial(
                factory, project_id=project,
                now=due + timedelta(minutes=offset),
            )
        async with system_project_session_scope(factory, project) as session:
            rows = (await session.scalars(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project,
                    OwnerNotificationDelivery.kind == "daily",
                )
            )).all()
            assert len(rows) == 1
            assert rows[0].telegram_id == owner_tid
            assert rows[0].event_key == delivery_day.isoformat()
            assert rows[0].payload == {
                "local_date": delivery_day.isoformat(),
                "timezone": "Europe/Moscow",
            }
            assert (await session.scalars(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == unrelated,
                )
            )).all() == []

        bot = SimpleNamespace(send_message=AsyncMock())
        assert await deliver_one_owner_notification(
            factory, project_id=project, bot=bot,
            now=due + timedelta(minutes=5),
        )
        assert not await deliver_one_owner_notification(
            factory, project_id=project, bot=bot,
            now=due + timedelta(minutes=6),
        )
        bot.send_message.assert_awaited_once()
        msg = bot.send_message.await_args.args[1]
        assert "Оплат подтверждено: 1" in msg
        assert "550 RUB" in msg
        assert "Заявок на проверке: 1" in msg
        assert "Активных платных подписчиков: 1" in msg
        assert "Daily revenue &lt;test&gt;" in msg
        callbacks = [
            b.callback_data
            for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert callbacks == ["own:statistics:7"]

        # Off after queue, before delivery, must suppress sending a fresh
        # next-day digest without mutating any financial records.
        upcoming = due + timedelta(days=1, minutes=1)
        await schedule_owner_daily_financial(
            factory, project_id=project, now=upcoming,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            await set_owner_setting(
                session, project_id=project, actor_id=owner,
                category="daily", enabled=False,
            )
        assert await prepare_owner_daily_financial(
            factory, project_id=project, recipient=owner_tid,
            payload={
                "local_date": (delivery_day + timedelta(days=1)).isoformat(),
                "timezone": "Europe/Moscow",
            }, now=upcoming,
        ) is None
        # No cross-owner delivery.
        assert await prepare_owner_daily_financial(
            factory, project_id=project, recipient=paid_tid,
            payload={
                "local_date": delivery_day.isoformat(),
                "timezone": "Europe/Moscow",
            }, now=due + timedelta(minutes=6),
        ) is None
    finally:
        await engine.dispose()
