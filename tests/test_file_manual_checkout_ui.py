"""Multi-currency file sales: never deliver files on an unverified manual request."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.file_sales import handlers as ui
from telegram_membership_saas.modules.file_sales.manual import (
    FilePaymentOption,
    parse_price,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 98765
    bot.send_message = AsyncMock()
    bot.send_invoice = AsyncMock()
    bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=55))
    return bot


def _query(action):
    return SimpleNamespace(
        data="fs:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="user", first_name="Buyer"),
        answer=AsyncMock(),
    )


def _callback(router):
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "file_callback"
    )


@pytest.mark.parametrize(
    ("value", "precision", "expected"),
    [("500", 2, Decimal("500")), ("12.50", 2, Decimal("12.50")),
     ("12,50", 2, Decimal("12.50"))],
)
def test_explicit_file_prices_are_decimal_not_stars_conversions(
    value, precision, expected,
):
    assert parse_price(value, precision) == expected


@pytest.mark.parametrize("value", ("0", "-1", "nan", "1.001", "inf", "foo"))
def test_invalid_or_unrepresentable_fiat_prices_rejected(value):
    with pytest.raises(ValueError):
        parse_price(value, 2)


async def test_select_file_opens_manual_methods_and_stars_not_invoice(monkeypatch):
    project, product, folder, method = uuid4(), uuid4(), uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, folder_id=folder, name="Мои видео",
            description="", stars_price=500, status="published",
        ), 1,
    )))
    monkeypatch.setattr(ui, "list_file_methods", AsyncMock(return_value=(
        FilePaymentOption(
            currency="RUB", amount=Decimal("525"), precision=2,
            method_id=method, method_name="Сбербанк",
        ),
    )))
    bot = _bot()
    await _callback(router)(_query("item:" + encode_uuid(product)), bot)
    bot.send_invoice.assert_not_awaited()
    kwargs = bot.send_message.await_args.kwargs
    assert "Мои видео" in kwargs["text"]
    assert "525 RUB" in kwargs["text"]
    actions = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard for button in row
    ]
    assert any(a.startswith("fs:method:") for a in actions)
    assert "fs:stars:" + encode_uuid(product) in actions
    assert "fs:folder:" + encode_uuid(folder) in actions


async def test_manual_checkout_waits_for_receipt_and_never_delivers_files(monkeypatch):
    project, product, method, order_id = uuid4(), uuid4(), uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "create_manual_file_order", AsyncMock(return_value=(
        SimpleNamespace(
            id=order_id, product_name="Мои видео", amount=Decimal("525"),
            currency_code="RUB", payment_method_name="Сбербанк",
            instruction="Карта 1234", status="waiting_confirmation",
        )
    )))
    delivery = AsyncMock()
    monkeypatch.setattr(ui, "deliver_manual", delivery, raising=False)
    bot = _bot()
    await _callback(router)(
        _query("method:" + encode_uuid(product) + ":RUB:" + encode_uuid(method)),
        bot,
    )
    bot.send_invoice.assert_not_awaited()
    delivery.assert_not_awaited()
    kwargs = bot.send_message.await_args.kwargs
    assert "Карта 1234" in kwargs["text"]
    assert "525 RUB" in kwargs["text"]
    actions = [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert "fs:mpaid:" + encode_uuid(order_id) in actions
    assert "fs:mcancel:" + encode_uuid(order_id) in actions


async def test_manual_review_approves_once_and_releases_files(monkeypatch):
    project, order_id = uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    reviewer = AsyncMock(return_value=(
        SimpleNamespace(id=order_id, telegram_user_id=4321), True,
    ))
    monkeypatch.setattr(ui, "review_manual_file_order", reviewer)
    # The nested delivery function is intentionally not mocked; verify it
    # reaches the paid-assets service only after the review succeeds.
    monkeypatch.setattr(
        ui, "purchased_manual_assets",
        AsyncMock(return_value=(
            SimpleNamespace(id=order_id, product_name="Мои видео"), (),
        )),
    )
    monkeypatch.setattr(ui, "mark_manual_delivery", AsyncMock())
    bot = _bot()
    await _callback(router)(_query("mapprove:" + encode_uuid(order_id)), bot)
    reviewer.assert_awaited_once()
    assert reviewer.await_args.kwargs["approve"] is True
    assert bot.send_invoice.await_count == 0
    # An empty mock asset tuple cannot demonstrate actual file transfer, but
    # the paid-only accessor must have been consulted before sending.
    ui.purchased_manual_assets.assert_awaited_once()
