"""Owner invoice notification contract: no false payment, tenant RLS, RU/EN."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

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
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_notifications import (
    OwnerNotificationDelivery,
    _invoice_keyboard,
    _invoice_text,
    deliver_one_owner_notification,
    queue_invoice_created,
    read_owner_settings,
    set_owner_setting,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext


def _payload():
    return {
        "request_id": uuid4().hex,
        "customer_id": uuid4().hex,
        "username": "buyer<script>",
        "first_name": "Alex & Co",
        "plan_name": "1 месяц",
        "duration_value": 1,
        "duration_unit": "month",
        "amount": "500",
        "currency": "RUB",
        "method": "Bank",
    }


@pytest.mark.parametrize(
    ("language", "heading", "awaiting", "period"),
    [
        ("ru", "Счёт сформирован", "Ожидает оплату", "1 месяц"),
        ("en", "Payment invoice created", "Awaiting payment", "1 month"),
    ],
)
def test_invoice_text_is_localized_safe_and_not_paid(language, heading, awaiting, period):
    token = _language.set(language)
    try:
        text = _invoice_text(_payload())
        assert heading in text
        assert awaiting in text
        assert period in text
        assert "500 RUB" in text
        assert "&lt;script&gt;" in text
        assert "&amp; Co" in text
        assert "Оплата подтверждена" not in text
        callbacks = [
            b.callback_data for row in _invoice_keyboard(_payload()).inline_keyboard for b in row
        ]
        assert all(len(callback) <= 64 for callback in callbacks)
        assert "own:reviews" in callbacks
    finally:
        _language.reset(token)


async def _user(factory, telegram_id):
    async with factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=telegram_id)
            session.add(user)
            await session.flush()
            return user.id


@pytest.mark.skipif(not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL")
async def test_durable_invoice_queue_owner_only_and_project_rls():
    settings = Settings(_env_file=None)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    owner_telegram_id = (uuid4().int % 7_000_000_000_000_000_000) + 1
    owner_id = await _user(factory, owner_telegram_id)
    outsider_id = await _user(factory, owner_telegram_id + 1)
    try:
        async with tenant_session_scope(factory, TenantContext(user_id=owner_id)) as session:
            project = Project(owner_user_id=owner_id, name="Test notifications")
            session.add(project)
            await session.flush()
            project_id = project.id
        request_id, customer_id = uuid4(), uuid4()
        values = dict(
            project_id=project_id, request_id=request_id, customer_id=customer_id,
            username="buyer", first_name="Alex", plan_name="One month",
            duration_value=1, duration_unit="month", amount="500",
            currency="RUB", method="Bank",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            assert (await read_owner_settings(session, project_id))["invoice"]
            await queue_invoice_created(session, **values)
            await queue_invoice_created(session, **values)
            rows = (
                await session.scalars(
                    select(OwnerNotificationDelivery).where(
                        OwnerNotificationDelivery.project_id == project_id,
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].telegram_id == owner_telegram_id

        bot = SimpleNamespace(send_message=AsyncMock())
        assert await deliver_one_owner_notification(factory, project_id=project_id, bot=bot)
        assert not await deliver_one_owner_notification(factory, project_id=project_id, bot=bot)
        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.args[1]
        assert "Счёт сформирован" in text

        with pytest.raises(TenantAccessDenied):
            async with tenant_session_scope(
                factory, TenantContext(user_id=outsider_id, project_id=project_id),
            ):
                pass
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            with pytest.raises(PermissionError):
                await set_owner_setting(
                    session, project_id=project_id, actor_id=outsider_id,
                    category="invoice", enabled=False,
                )
            await set_owner_setting(
                session, project_id=project_id, actor_id=owner_id,
                category="invoice", enabled=False,
            )
            assert not (await read_owner_settings(session, project_id))["invoice"]
            await queue_invoice_created(
                session, **{**values, "request_id": uuid4()},
            )
            rows = (
                await session.scalars(
                    select(OwnerNotificationDelivery).where(
                        OwnerNotificationDelivery.project_id == project_id,
                    )
                )
            ).all()
            assert len(rows) == 1
    finally:
        await engine.dispose()
