"""Connected file shop must invoice Stars and never unlock unpaid media."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from test_owner_subscribers_ui import _bot

from telegram_membership_saas.modules.file_sales import handlers as ui
from telegram_membership_saas.modules.file_sales.service import DigitalOrder
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def query(action):
    return SimpleNamespace(
        data="fs:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
        answer=AsyncMock(),
    )


def cb(router):
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "file_callback"
    )


async def test_buyer_gets_xtr_invoice_but_not_file_before_stars_confirmation(
    monkeypatch,
):
    project, product, purchase = uuid4(), uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    order = DigitalOrder(
        id=purchase, product_id=product, product_name="Book",
        amount=79, invoice_payload="fs:" + purchase.hex, paid=False,
    )
    create = AsyncMock(return_value=order)
    monkeypatch.setattr(ui, "create_stars_order", create)
    delivered = AsyncMock()
    monkeypatch.setattr(ui, "purchased_assets", delivered)
    bot = _bot()
    bot.id = 9999
    bot.send_invoice = AsyncMock()
    await cb(router)(query("buy:" + encode_uuid(product)), bot)
    bot.send_invoice.assert_awaited_once()
    args = bot.send_invoice.await_args.kwargs
    assert args["currency"] == "XTR"
    assert args["provider_token"] == ""
    assert len(args["prices"]) == 1
    assert args["prices"][0].amount == 79
    assert args["payload"] == order.invoice_payload
    delivered.assert_not_awaited()


async def test_confirmed_charge_only_unlocks_files_to_actual_buyer(monkeypatch):
    project, purchase = uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    bot = _bot()
    bot.id = 9999
    payment = SimpleNamespace(
        invoice_payload="fs:" + purchase.hex,
        currency="XTR", total_amount=79,
        telegram_payment_charge_id="t_charge_id",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(type="private", id=123),
        date=datetime.now(UTC),
        successful_payment=payment,
    )
    confirmed = AsyncMock(return_value=SimpleNamespace(id=purchase))
    monkeypatch.setattr(ui, "confirm_stars_payment", confirmed)
    delivered = AsyncMock()
    monkeypatch.setattr(ui, "purchased_assets", AsyncMock(
        return_value=(SimpleNamespace(id=purchase, product_name="Book"), (
            SimpleNamespace(kind="document", file_id="secret_doc"),
        )),
    ))
    monkeypatch.setattr(ui, "mark_file_delivery", delivered)
    bot.send_document = AsyncMock()
    handler = next(
        r.callback for r in router.message.handlers
        if r.callback.__name__ == "file_payment_success"
    )
    await handler(message, bot)
    confirmed.assert_awaited_once()
    assert confirmed.await_args.kwargs["buyer_id"] == 123
    assert confirmed.await_args.kwargs["charge_id"] == "t_charge_id"
    assert confirmed.await_args.kwargs["payment_at"] == message.date
    bot.send_document.assert_awaited_once_with(123, "secret_doc")
    delivered.assert_awaited_once()
    assert delivered.await_args.kwargs["purchase_id"] == purchase


async def test_unauthorized_owner_controls_are_denied(monkeypatch):
    router = ui.create_file_sales_router(MagicMock(), project_id=uuid4())
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    add = AsyncMock()
    monkeypatch.setattr(ui, "add_folder", add)
    request = query("newfolder")
    await cb(router)(request, _bot())
    assert request.answer.await_args.kwargs["show_alert"] is True
    add.assert_not_awaited()


async def test_wrong_stars_checkout_is_declined_without_delivery(monkeypatch):
    router = ui.create_file_sales_router(MagicMock(), project_id=uuid4())
    denied = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "precheckout_allowed", denied)
    bot = _bot()
    bot.id = 9999
    bot.answer_pre_checkout_query = AsyncMock()
    msg = SimpleNamespace(
        invoice_payload="fs:untrusted",
        from_user=SimpleNamespace(id=123),
        total_amount=42, currency="XTR", id="checkout",
    )
    handler = next(
        h.callback for h in router.pre_checkout_query.handlers
        if h.callback.__name__ == "file_pre_checkout"
    )
    await handler(msg, bot)
    assert denied.await_args.kwargs["invoice_payload"] == "fs:untrusted"
    assert bot.answer_pre_checkout_query.await_args.kwargs["ok"] is False


async def test_editor_uploads_media_but_only_after_owner_recheck(monkeypatch):
    project, product = uuid4(), uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "can_manage_project", allowed)
    monkeypatch.setattr(ui, "view_product", AsyncMock(
        return_value=(SimpleNamespace(id=product), 0),
    ))
    add = AsyncMock(return_value=1)
    monkeypatch.setattr(ui, "add_product_asset", add)
    bot = _bot()
    bot.id = 9999
    await cb(router)(query("upload:" + encode_uuid(product)), bot)
    edit = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "editor_message"
    )
    media = SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        chat=SimpleNamespace(type="private", id=123),
        text=None, document=SimpleNamespace(file_id="doc", mime_type="application/pdf"),
        photo=None, video=None, animation=None, audio=None,
        has_protected_content=False,
    )
    allowed.return_value = False
    await edit(media, bot)
    add.assert_not_awaited()


async def test_active_owner_editor_does_not_capture_a_payment_update():
    from time import monotonic

    states = {123: {"stage": "upload", "expires": monotonic() + 600}}
    matcher = ui.FileEditorFilter(states, MagicMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(type="private", id=123),
        text=None,
        successful_payment=SimpleNamespace(invoice_payload="fs:test"),
    )
    assert not await matcher(message, MagicMock())
    assert 123 in states


async def test_paid_files_history_paginates_instead_of_hiding_old_receipts(
    monkeypatch,
):
    project = uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    fetch = AsyncMock(return_value=(31, tuple(
        SimpleNamespace(id=uuid4(), product_name="Old paid file")
        for _ in range(8)
    )))
    monkeypatch.setattr(ui, "list_purchases_page", fetch)
    bot = _bot()
    bot.id = 9999
    await cb(router)(query("mine:1"), bot)
    assert fetch.await_args.kwargs["page"] == 1
    keys = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for button in row
    ]
    assert "fs:mine:0" in keys
    assert "fs:mine:2" in keys


async def test_stars_charge_db_failure_does_not_claim_paid_delivery(
    monkeypatch,
):
    project = uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    bot = _bot()
    bot.id = 9999
    confirmed = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(ui, "confirm_stars_payment", confirmed)
    deliver = AsyncMock()
    monkeypatch.setattr(ui, "purchased_assets", deliver)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(type="private", id=123),
        successful_payment=SimpleNamespace(
            invoice_payload="fs:" + uuid4().hex,
            currency="XTR", total_amount=40,
            telegram_payment_charge_id="paid_charge",
        ),
    )
    handler = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "file_payment_success"
    )
    await handler(message, bot)
    deliver.assert_not_awaited()
    sent = bot.send_message.await_args.args[1]
    assert "не смог записать" in sent
    assert "Проверить оплату" in sent
