from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import ForceReply

from telegram_membership_saas.modules.telegram_commerce import handlers as receipt_handlers
from telegram_membership_saas.modules.telegram_commerce import service as commerce_service
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    SubmissionResult,
    encode_uuid,
)


@asynccontextmanager
async def _scope(*_args, **_kwargs):
    yield object()


def _bot():
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock()
    return bot


def _callback(request_id, user_id=100):
    callback = SimpleNamespace(
        data=f"paid:{encode_uuid(request_id)}",
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )
    return callback


def _message(request_id, *, user_id=100, mime_type=None, photo=False, bot_id=123456):
    prompt = SimpleNamespace(
        from_user=SimpleNamespace(id=bot_id),
        text=f"{receipt_handlers.RECEIPT_REPLY_PREFIX}{request_id}\nReply with file",
    )
    document = SimpleNamespace(mime_type=mime_type) if mime_type else None
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type="private", id=user_id),
        reply_to_message=prompt,
        message_id=37,
        photo=[object()] if photo else None,
        document=document,
    )


def _handler(router, name, *, callback=False):
    observer = router.callback_query if callback else router.message
    return next(item.callback for item in observer.handlers if item.callback.__name__ == name)


async def test_paid_button_does_not_submit_without_receipt(monkeypatch):
    request_id = uuid4()
    project_id = uuid4()
    gate = AsyncMock()
    submit = AsyncMock()
    monkeypatch.setattr(receipt_handlers, "system_project_session_scope", _scope)
    monkeypatch.setattr(receipt_handlers, "require_awaiting_receipt_for_telegram_user", gate)
    monkeypatch.setattr(receipt_handlers, "submit_checkout_for_telegram_user", submit)
    handler = _handler(
        receipt_handlers.create_telegram_commerce_router(None, project_id=project_id),
        "paid_handler",
        callback=True,
    )
    callback = _callback(request_id)
    bot = _bot()

    await handler(callback, bot)

    gate.assert_awaited_once()
    submit.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert isinstance(markup, ForceReply)
    assert str(request_id) in bot.send_message.await_args.kwargs["text"]
    callback.answer.assert_awaited_once()


async def test_foreign_receipt_prompt_is_rejected(monkeypatch):
    request_id = uuid4()
    submit = AsyncMock()
    monkeypatch.setattr(receipt_handlers, "submit_checkout_for_telegram_user", submit)
    handler = _handler(
        receipt_handlers.create_telegram_commerce_router(None, project_id=uuid4()),
        "receipt_handler",
    )
    message = _message(request_id, photo=True, bot_id=999)
    bot = _bot()

    await handler(message, bot)

    submit.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.copy_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("photo", "mime_type"),
    [
        (False, None),
        (False, "text/plain"),
        (False, "application/octet-stream"),
    ],
)
async def test_text_and_unsupported_receipts_do_not_submit(monkeypatch, photo, mime_type):
    request_id = uuid4()
    submit = AsyncMock()
    monkeypatch.setattr(receipt_handlers, "submit_checkout_for_telegram_user", submit)
    handler = _handler(
        receipt_handlers.create_telegram_commerce_router(None, project_id=uuid4()),
        "receipt_handler",
    )
    bot = _bot()

    await handler(_message(request_id, photo=photo, mime_type=mime_type), bot)

    submit.assert_not_awaited()
    bot.copy_message.assert_not_awaited()
    assert "Нужен файл" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.parametrize(
    ("photo", "mime_type"),
    [
        (True, None),
        (False, "application/pdf"),
        (False, "image/jpeg"),
    ],
)
async def test_receipt_is_copied_to_reviewer_without_persisting_file_data(
    monkeypatch, photo, mime_type
):
    request_id = uuid4()
    project_id = uuid4()
    buyer_id = 100
    reviewer_id = 200
    submission = SubmissionResult(
        request=SimpleNamespace(id=request_id),
        item=ReviewItem(
            request_id=request_id,
            customer_telegram_user_id=buyer_id,
            customer_username="buyer",
            plan_name="Premium",
            amount=Decimal("10"),
            currency_code="RUB",
            payment_method_name="Manual",
            status="waiting_review",
        ),
        reviewer_telegram_ids=(reviewer_id,),
        newly_submitted=True,
    )
    submit = AsyncMock(return_value=submission)
    monkeypatch.setattr(receipt_handlers, "system_project_session_scope", _scope)
    monkeypatch.setattr(receipt_handlers, "submit_checkout_for_telegram_user", submit)
    handler = _handler(
        receipt_handlers.create_telegram_commerce_router(None, project_id=project_id),
        "receipt_handler",
    )
    bot = _bot()

    await handler(_message(request_id, photo=photo, mime_type=mime_type), bot)

    submit.assert_awaited_once()
    assert submit.await_args.kwargs["telegram_user_id"] == buyer_id
    bot.copy_message.assert_awaited_once_with(
        chat_id=reviewer_id,
        from_chat_id=buyer_id,
        message_id=37,
    )
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].kwargs["chat_id"] == reviewer_id
    assert "документ отправлены" in bot.send_message.await_args_list[1].kwargs["text"]


async def test_payment_request_requires_buyer_and_waiting_confirmation(monkeypatch):
    project_id = uuid4()
    request_id = uuid4()
    customer_id = uuid4()
    monkeypatch.setattr(commerce_service, "_require_active_project", AsyncMock())
    monkeypatch.setattr(
        commerce_service,
        "_customer_for_telegram_user",
        AsyncMock(return_value=SimpleNamespace(id=customer_id)),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(customer_id=customer_id, status="waiting_review")
        )
    )
    with pytest.raises(ValueError, match="уже отправлена"):
        await commerce_service.require_awaiting_receipt_for_telegram_user(
            session,
            project_id=project_id,
            request_id=request_id,
            telegram_user_id=100,
        )
    session.scalar.return_value.status = "waiting_confirmation"
    await commerce_service.require_awaiting_receipt_for_telegram_user(
        session,
        project_id=project_id,
        request_id=request_id,
        telegram_user_id=100,
    )
    session.scalar.return_value.customer_id = uuid4()
    with pytest.raises(PermissionError):
        await commerce_service.require_awaiting_receipt_for_telegram_user(
            session,
            project_id=project_id,
            request_id=request_id,
            telegram_user_id=100,
        )
