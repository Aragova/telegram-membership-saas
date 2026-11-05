"""PR #59: owner paid-term digests, 10:00 IANA timezone, RU/EN and safety."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from telegram_membership_saas import telegram_runtime
from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_expiry import (
    PaidCoverage,
    PaidDeadline,
    classify_paid_deadlines,
    exclude_replaced_deadlines,
    next_digest_check,
    render_expiry_digest,
    schedule_owner_expiry_digest,
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
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantContext


def _deadline(customer, when, status="active", *, username="buyer",
              plan_name="Premium <Gold>", start=None, plan=None):
    return PaidDeadline(
        customer_id=customer, plan_id=plan or uuid4(), ends_at=when,
        status=status, started_at=start or when - timedelta(days=30),
        username=username, first_name="Buyer", plan_name=plan_name,
    )


@pytest.mark.parametrize(
    ("timezone", "before", "target"),
    [
        ("Europe/Moscow", datetime(2026, 11, 5, 6, 59, tzinfo=UTC),
         datetime(2026, 11, 5, 7, tzinfo=UTC)),
        ("Europe/Amsterdam", datetime(2026, 3, 29, 7, 59, tzinfo=UTC),
         datetime(2026, 3, 29, 8, tzinfo=UTC)),
        ("Europe/Amsterdam", datetime(2026, 10, 25, 8, 59, tzinfo=UTC),
         datetime(2026, 10, 25, 9, tzinfo=UTC)),
    ],
)
def test_exact_ten_am_timezone_and_dst(timezone, before, target):
    assert next_digest_check(before, timezone) == target
    assert next_digest_check(target, timezone) == (
        datetime.combine(
            target.astimezone(ZoneInfo(timezone)).date() + timedelta(days=1),
            time(10), tzinfo=ZoneInfo(timezone),
        ).astimezone(UTC)
    )


def test_reminder_days_expired_no_lifetime_no_manual_and_dedupe():
    zone = ZoneInfo("Europe/Moscow")
    today = date(2026, 11, 6)
    def at(days, hour=12):
        return datetime.combine(
            today + timedelta(days=days), time(hour), tzinfo=zone,
        ).astimezone(UTC)
    first, second, third, fourth = (uuid4() for _ in range(4))
    rows = [
        _deadline(first, at(7), username="alice"),
        _deadline(second, at(3), username="bob"),
        _deadline(second, at(3, 13), username="bob"),  # distinct buyer
        _deadline(third, at(1), username="charlie"),
        _deadline(fourth, at(-1, 12), status="expired", username="dave"),
        _deadline(uuid4(), at(2)),  # no reminder on day two
        _deadline(uuid4(), at(-2)),  # already ended, not today's incident
        _deadline(uuid4(), at(3), status="cancelled"),
        _deadline(uuid4(), at(7), start=at(1)),
    ]
    groups = classify_paid_deadlines(
        rows, local_date=today, timezone="Europe/Moscow",
    )
    assert {k: len(v) for k, v in groups.items()} == {
        7: 1, 3: 1, 1: 1, 0: 1,
    }
    assert groups[3][0].customer_id == second
    assert groups[0][0].customer_id == fourth


@pytest.mark.parametrize(
    ("language", "title", "expired", "label"),
    [
        ("ru", "Сроки подписок", "Оплаченный срок истёк", "Через 3 дн."),
        ("en", "Subscription deadlines", "Paid term ended", "In 3 days"),
    ],
)
def test_localized_digest_escapes_dynamic_names_and_no_access_claim(
    language, title, expired, label,
):
    day = date(2026, 11, 6)
    zone = ZoneInfo("Europe/Moscow")
    group = {
        7: [],
        3: [_deadline(uuid4(), datetime(2026, 11, 9, 12, tzinfo=zone),
                      username="a<script>")],
        1: [],
        0: [_deadline(uuid4(), datetime(2026, 11, 5, 11, tzinfo=zone),
                      status="expired", username="b&c")],
    }
    token = _language.set(language)
    try:
        message = render_expiry_digest(
            group, local_date=day, timezone="Europe/Moscow",
        )
        assert message is not None
        assert title in message
        assert expired in message
        assert label in message
        assert "&lt;script&gt;" in message
        assert "Premium &lt;Gold&gt;" in message
        assert "b&amp;c" in message
        assert len(message) < 4096
        assert not any(x in message.lower() for x in ("удалён из канала", "removed from channel"))
    finally:
        _language.reset(token)


def test_digests_with_no_paid_terms_are_silent():
    assert render_expiry_digest(
        {7: [], 3: [], 1: [], 0: []},
        local_date=date(2026, 11, 6), timezone="Europe/Moscow",
    ) is None


@pytest.mark.skipif(not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL")
async def test_owner_ten_am_digest_dedup_and_renewal_rechecked_before_delivery():
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    tz = ZoneInfo("Europe/Moscow")
    day = datetime.now(tz).date() + timedelta(days=1)
    due = datetime.combine(day, time(10), tzinfo=tz).astimezone(UTC)
    try:
        def tid():
            return uuid4().int % 8_000_000_000_000_000_000 + 1

        owner_tid = tid()
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tid, username="expiry_owner", first_name="Owner",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Daily expiry integration",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            await set_owner_setting(
                session, project_id=project, actor_id=owner,
                category="invoice", enabled=False,
            )
            resource_id = uuid4()
            session.add(Resource(
                id=resource_id, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Expiry test resource",
            ))
        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Expiring Premium", duration_value=1,
            duration_unit="month", resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="RUB", amount="100",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Bank", currency_code="RUB", instruction="Test transfer",
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        buyers = []
        for number in (1, 2):
            buyer = tid()
            async with system_project_session_scope(factory, project) as session:
                option = (await list_checkout_options(
                    session, project_id=project,
                ))[0]
                checkout = await create_checkout_for_telegram_user(
                    session, project_id=project, telegram_user_id=buyer,
                    username=f"expiry_buyer_{number}", first_name="Buyer",
                    plan_price_id=option.plan_price_id,
                    payment_method_id=option.payment_method_id,
                )
            async with system_project_session_scope(factory, project) as session:
                await submit_checkout_for_telegram_user(
                    session, project_id=project,
                    request_id=checkout.request.id, telegram_user_id=buyer,
                )
                review = await review_payment_request_from_telegram(
                    session, project_id=project, request_id=checkout.request.id,
                    actor_telegram_user_id=owner_tid, approve=True,
                )
                assert review.changed
            buyers.append(buyer)

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            sub = list((await session.scalars(
                select(Subscription).where(Subscription.project_id == project)
            )).all())
            assert len(sub) == 2
            end = datetime.combine(
                day + timedelta(days=3), time(12), tzinfo=tz,
            ).astimezone(UTC)
            for item in sub:
                item.current_period_end = end

        before = await schedule_owner_expiry_digest(
            factory, project_id=project, now=due - timedelta(minutes=1),
        )
        assert before == due
        async with system_project_session_scope(factory, project) as session:
            assert (await session.scalars(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project,
                )
            )).all() == []

        await schedule_owner_expiry_digest(
            factory, project_id=project, now=due + timedelta(minutes=1),
        )
        await schedule_owner_expiry_digest(
            factory, project_id=project, now=due + timedelta(minutes=2),
        )
        async with system_project_session_scope(factory, project) as session:
            ledger = list((await session.scalars(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project,
                )
            )).all())
            assert len(ledger) == 1
            assert ledger[0].kind == "expiry"
            assert ledger[0].event_key == day.isoformat()
            assert ledger[0].telegram_id == owner_tid

        # Simulate renewal after scheduling but before dispatch. Only the
        # genuinely expiring paid term must appear in the Telegram message.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            active = list((await session.scalars(
                select(Subscription).where(Subscription.project_id == project)
            )).all())
            active[0].current_period_end += timedelta(days=30)

        from types import SimpleNamespace

        bot = SimpleNamespace(send_message=AsyncMock())
        assert await deliver_one_owner_notification(
            factory, project_id=project, bot=bot,
            now=due + timedelta(minutes=3),
        )
        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.args[1]
        assert "Сроки подписок" in text
        assert "Через 3 дн." in text
        assert text.count("• @expiry_buyer_") == 1

        # Yesterday's durable digest must never claim today's still-ongoing
        # deadline if Telegram delivery was delayed into the next local day.
        assert not await deliver_one_owner_notification(
            factory, project_id=project, bot=bot,
            now=due + timedelta(days=1),
        )
    finally:
        await engine.dispose()



def test_cross_plan_paid_replacement_suppresses_only_fully_covered_channels():
    """A lifetime/annual upgrade must suppress an old monthly deadline."""
    customer, stranger = uuid4(), uuid4()
    old_plan, new_annual, new_lifetime, partial = (
        uuid4() for _ in range(4)
    )
    channel_a, channel_b, other_channel = (uuid4() for _ in range(3))
    now = datetime(2026, 11, 6, 7, tzinfo=UTC)
    ends = now + timedelta(days=3)
    old = _deadline(
        customer, ends, plan=old_plan, start=now - timedelta(days=27),
    )
    resources = {
        old_plan: {channel_a, channel_b},
        new_annual: {channel_a, channel_b},
        new_lifetime: {channel_a, channel_b},
        partial: {channel_a, other_channel},
    }

    assert exclude_replaced_deadlines(
        [old],
        coverages=[PaidCoverage(
            customer, new_annual, ends + timedelta(days=300), now,
        )],
        plan_resources=resources, checked_at=now,
    ) == []
    assert exclude_replaced_deadlines(
        [old],
        coverages=[PaidCoverage(
            customer, new_lifetime, None, now - timedelta(days=1),
        )],
        plan_resources=resources, checked_at=now,
    ) == []
    assert exclude_replaced_deadlines(
        [old],
        coverages=[PaidCoverage(
            customer, partial, None, now - timedelta(days=1),
        )],
        plan_resources=resources, checked_at=now,
    ) == [old]  # channel B is unprotected
    assert exclude_replaced_deadlines(
        [old],
        coverages=[PaidCoverage(
            stranger, new_lifetime, None, now - timedelta(days=1),
        )],
        plan_resources=resources, checked_at=now,
    ) == [old]  # never borrow another buyer's paid rights
    assert exclude_replaced_deadlines(
        [old],
        coverages=[PaidCoverage(
            customer, new_lifetime, None, now + timedelta(days=1),
        )],
        plan_resources=resources, checked_at=now,
    ) == [old]  # future paid start does not yet cover the old plan


async def test_running_worker_reschedules_when_owner_changes_timezone(monkeypatch):
    """A live Moscow→Tokyo edit cannot use the old 07:00 UTC deadline."""
    moment = datetime(2026, 11, 5, 0, tzinfo=UTC)
    clock = iter([moment, moment + timedelta(seconds=61)])
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=["Europe/Moscow", "Asia/Tokyo"])

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    schedule = AsyncMock(return_value=moment + timedelta(days=1))
    monkeypatch.setattr(telegram_runtime, "system_project_session_scope", scope)
    monkeypatch.setattr(
        telegram_runtime, "schedule_owner_expiry_digest", schedule,
    )
    monkeypatch.setattr(
        telegram_runtime, "reconcile_owner_service", AsyncMock(return_value=0),
    )
    work = AsyncMock(side_effect=[False, asyncio.CancelledError()])
    monkeypatch.setattr(telegram_runtime, "process_project_runtime_once", work)

    with pytest.raises(asyncio.CancelledError):
        await telegram_runtime.run_project_runtime_loop(
            MagicMock(), project_id=uuid4(), adapter=MagicMock(),
            poll_seconds=0.001, clock=lambda: next(clock),
        )
    assert schedule.await_count == 2
    assert session.scalar.await_count == 2
