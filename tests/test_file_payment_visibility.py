"""File sale payment switches are per-bot and never disable subscriptions."""
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales import handlers as ui
from telegram_membership_saas.modules.file_sales import manual_service as manual
from telegram_membership_saas.modules.file_sales import service as files
from telegram_membership_saas.modules.file_sales.payment_controls import (
    AUTO_STARS_ID,
    FilePaymentChoice,
    file_stars_enabled,
    list_file_payment_controls,
    set_all_file_payment_methods,
    toggle_file_payment_method,
)
from telegram_membership_saas.modules.payments import PaymentMethod, PaymentMethodCurrency
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext


def _query(action):
    return SimpleNamespace(
        data="fs:" + action,
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_file_payment_switcher_has_one_dynamic_bulk_button(monkeypatch):
    project, method_id = uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    handler = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "file_callback"
    )
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "can_manage_project", AsyncMock(return_value=True),
    )
    rows_on = (
        FilePaymentChoice(method_id, "Telegram Stars ⭐", True),
        FilePaymentChoice(AUTO_STARS_ID, "Telegram Stars (XTR)", True, True),
    )
    rows_off = (
        FilePaymentChoice(method_id, "Telegram Stars ⭐", False),
        FilePaymentChoice(AUTO_STARS_ID, "Telegram Stars (XTR)", True, True),
    )
    listed = AsyncMock(side_effect=(rows_on, rows_off, rows_on))
    monkeypatch.setattr(ui, "list_file_payment_controls", listed)
    toggle = AsyncMock(return_value=False)
    bulk = AsyncMock()
    monkeypatch.setattr(ui, "toggle_file_payment_method", toggle)
    monkeypatch.setattr(ui, "set_all_file_payment_methods", bulk)
    bot = MagicMock()
    bot.id = 9999
    bot.send_message = AsyncMock()

    await handler(_query("paymethods"), bot)
    buttons = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    assert len(buttons) == 4
    assert any("Telegram Stars ⭐" in b.text for b in buttons)
    assert any("Автоматические Stars" in b.text for b in buttons)
    assert [b.callback_data for b in buttons if "paybulk" in b.callback_data] == [
        "fs:paybulk:off",
    ]

    await handler(_query("paytoggle:" + method_id.hex), bot)
    toggle.assert_awaited_once()
    assert toggle.await_args.kwargs["method_id"] == method_id
    buttons = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    assert [b.callback_data for b in buttons if "paybulk" in b.callback_data] == [
        "fs:paybulk:on",
    ]
    await handler(_query("paybulk:on"), bot)
    bulk.assert_awaited_once()
    assert bulk.await_args.kwargs["enabled"] is True


async def test_non_owner_cannot_toggle_file_payments(monkeypatch):
    router = ui.create_file_sales_router(MagicMock(), project_id=uuid4())
    handler = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "file_callback"
    )
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "can_manage_project", AsyncMock(return_value=False),
    )
    bulk = AsyncMock()
    monkeypatch.setattr(ui, "set_all_file_payment_methods", bulk)
    bot = MagicMock()
    bot.id = 9999
    query = _query("paybulk:off")
    await handler(query, bot)
    bulk.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)
async def test_real_file_controls_are_per_bot_and_preserve_open_orders():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, _, _, _, buyer_id = (
            await setup_subscriber(factory)
        )
        bot_id, other_bot = tg_id(), tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            for tid in (bot_id, other_bot):
                session.add(BotInstance(
                    project_id=project, telegram_bot_id=tid,
                    username="file_switch_" + str(tid),
                    display_name="File switch test",
                    token_ciphertext="test-token",
                ))
            method = PaymentMethod(
                project_id=project, name="Сбербанк",
                instruction_template="Pay {amount} {currency}",
                status="active",
            )
            session.add(method)
            await session.flush()
            session.add(PaymentMethodCurrency(
                payment_method_id=method.id,
                project_id=project, currency_code="RUB",
            ))
            method_id = method.id

        folder = await files.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Downloads",
        )
        product = await files.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id,
            name="Video", stars=50,
        )
        await manual.set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, currency="RUB", value="100",
        )
        await files.add_product_asset(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            product_id=product.id, kind="document", file_id="VIDEO_TEST",
        )
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, status="published",
        )
        before = await manual.available_methods(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product.id,
        )
        assert len(before) == 1 and before[0].amount == Decimal("100")
        pending = await files.create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product.id,
        )
        assert await files.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_id,
            invoice_payload=pending.invoice_payload, amount=50, currency="XTR",
        )

        initial = await list_file_payment_controls(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
        )
        assert all(item.enabled for item in initial)
        assert method_id in {x.method_id for x in initial}
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await toggle_file_payment_method(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, method_id=method_id,
            )
        assert await toggle_file_payment_method(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, method_id=method_id,
        ) is False
        assert await manual.available_methods(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product.id,
        ) == ()
        with pytest.raises(ValueError, match="недоступны"):
            await manual.create_manual_order(
                factory, project_id=project, bot_id=bot_id, buyer_id=buyer_id,
                product_id=product.id, currency="RUB", method_id=method_id,
            )
        await set_all_file_payment_methods(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, enabled=False,
        )
        assert not await file_stars_enabled(
            factory, project_id=project, bot_id=bot_id,
        )
        assert not await files.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_id,
            invoice_payload=pending.invoice_payload, amount=50, currency="XTR",
        )
        with pytest.raises(ValueError, match="отключена"):
            await files.create_stars_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_id, product_id=product.id,
            )
        another = await list_file_payment_controls(
            factory, project_id=project, bot_id=other_bot, actor_id=owner,
        )
        assert all(x.enabled for x in another)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(PaymentMethod.status).where(
                PaymentMethod.id == method_id,
            )) == "active"

        await set_all_file_payment_methods(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, enabled=True,
        )
        assert all(x.enabled for x in await list_file_payment_controls(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
        ))
        assert len(await manual.available_methods(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product.id,
        )) == 1
        assert await files.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_id,
            invoice_payload=pending.invoice_payload, amount=50, currency="XTR",
        )
    finally:
        await engine.dispose()
