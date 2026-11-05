"""Owner invoice notification contract: no false payment, tenant RLS, RU/EN."""
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
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
from telegram_membership_saas.modules.owner_setup import handlers as ui
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
        assert any(value.startswith("own:invoice:") for value in callbacks)
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

@pytest.mark.parametrize(
    ("locale", "heading"),
    [("ru", "Уведомления владельца"), ("en", "Owner notifications")],
)
async def test_owner_notification_settings_ui_and_toggle_are_bilingual(
    monkeypatch, locale, heading,
):
    project, owner = uuid4(), uuid4()

    class FakeSession:
        scalar = AsyncMock(return_value=owner)

    @asynccontextmanager
    async def fake_scope(*_args, **_kwargs):
        yield FakeSession()

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "tenant_session_scope", fake_scope)
    flags = AsyncMock(return_value={
        "invoice": True, "receipt": True, "decision": True,
        "expiry": True, "access": True, "daily": False,
    })
    change = AsyncMock()
    monkeypatch.setattr(ui, "read_owner_settings", flags)
    monkeypatch.setattr(ui, "set_owner_setting", change)

    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    handle = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )

    def query(action):
        return SimpleNamespace(
            data="own:" + action,
            from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
            message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
            answer=AsyncMock(),
        )

    bot = SimpleNamespace(send_message=AsyncMock(), id=345)
    token = _language.set(locale)
    try:
        await handle(query("notifications"), bot)
        msg = bot.send_message.await_args.args[1]
        assert heading in msg
        assert "🧾" in msg
        assert (
            "New invoice" if locale == "en" else "Новый счёт"
        ) in msg
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        assert "own:notifyset:invoice:0" in [
            b.callback_data for row in markup.inline_keyboard for b in row
        ]

        await handle(query("notifyset:invoice:0"), bot)
        change.assert_awaited_once()
        assert change.await_args.kwargs["actor_id"] == owner
        assert change.await_args.kwargs["project_id"] == project
        assert change.await_args.kwargs["enabled"] is False
    finally:
        _language.reset(token)


async def test_central_bot_does_not_expose_invoice_notification_settings(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router = ui.create_owner_setup_router(MagicMock())
    handle = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    query = SimpleNamespace(
        data="own:notifications",
        from_user=SimpleNamespace(id=123, username="user", first_name="User"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), id=345)
    await handle(query, bot)
    bot.send_message.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"]



@pytest.mark.skipif(not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL")
async def test_invoice_flood_control_honors_telegram_retry_after():
    """Do not use the shorter default backoff when Telegram requires 15 minutes."""
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    owner_telegram_id = (uuid4().int % 7_000_000_000_000_000_000) + 1
    owner_id = await _user(factory, owner_telegram_id)
    try:
        async with tenant_session_scope(factory, TenantContext(user_id=owner_id)) as session:
            project = Project(owner_user_id=owner_id, name="Retry after tenant")
            session.add(project)
            await session.flush()
            project_id = project.id

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await queue_invoice_created(
                session, project_id=project_id,
                request_id=uuid4(), customer_id=uuid4(), username="buyer",
                first_name="Buyer", plan_name="One month",
                duration_value=1, duration_unit="month", amount="100",
                currency="RUB", method="Manual",
            )

        now = datetime.now(UTC)
        delay = 900
        retry = TelegramRetryAfter(
            method=SendMessage(chat_id=owner_telegram_id, text="Invoice"),
            message="Too Many Requests", retry_after=delay,
        )
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=retry))
        assert await deliver_one_owner_notification(
            factory, project_id=project_id, bot=bot, now=now,
        )
        async with system_project_session_scope(factory, project_id) as session:
            row = await session.scalar(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project_id,
                )
            )
            assert row is not None
            assert row.status == "pending"
            assert row.attempt_count == 1
            assert row.error_code == "TelegramRetryAfter"
            assert row.available_at >= now + timedelta(seconds=delay)

        assert not await deliver_one_owner_notification(
            factory, project_id=project_id, bot=bot,
            now=now + timedelta(minutes=5),
        )
        bot.send_message.assert_awaited_once()
    finally:
        await engine.dispose()
