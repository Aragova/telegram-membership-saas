"""RU/EN buyer checkout, review, receipt compatibility and access notifications."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas import localization
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.telegram_access.adapter import (
    TelegramAccessAdapter,
    TelegramAccessResult,
)
from telegram_membership_saas.modules.telegram_commerce import handlers as commerce
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    ReviewResult,
    encode_uuid,
)


def _item() -> ReviewItem:
    return ReviewItem(
        request_id=uuid4(), customer_telegram_user_id=300,
        customer_username="buyer", plan_name="Premium",
        amount=Decimal("19.95"), currency_code="EUR",
        payment_method_name="Owner's bank", status="waiting_review",
    )


def test_english_payment_and_review_texts() -> None:
    item = _item()
    result = ReviewResult(
        item=item, changed=True,
        subscription_period_end=datetime(2026, 12, 20, 10, tzinfo=UTC),
        project_timezone="Europe/Moscow",
    )
    token = _language.set("en")
    try:
        keyboard = commerce._checkout_keyboard(item.request_id)
        assert keyboard.inline_keyboard[0][0].text == "✅ I have paid"
        assert keyboard.inline_keyboard[1][0].text == "Cancel request"
        assert "Subscription activated" in commerce._customer_review_result_text(
            result, approve=True,
        )
        assert "Payment rejected" in commerce._customer_review_result_text(
            result, approve=False,
        )
        text = commerce._admin_review_result_text(result, approve=True)
        assert "Payment approved" in text
        assert "Customer: @buyer" in text
        assert "EUR" in text
        assert "Valid until:" in text
        assert commerce._buyer_error(ValueError("Заявка уже отправлена или закрыта")) == (
            "This payment request has already been submitted or closed."
        )
    finally:
        _language.reset(token)
    assert "Подписка активирована" in commerce._customer_review_result_text(
        result, approve=True,
    )


def test_legacy_and_english_receipt_reply_both_authorized() -> None:
    request_id = uuid4()
    for prefix in (commerce.RECEIPT_REPLY_PREFIX, commerce.RECEIPT_REPLY_PREFIX_EN):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            reply_to_message=SimpleNamespace(
                from_user=SimpleNamespace(id=700),
                text=f"{prefix}{request_id}\nOther text",
            ),
        )
        assert commerce._receipt_request_id(message, bot_id=700) == request_id
        assert commerce._receipt_request_id(message, bot_id=701) is None


async def test_admin_and_buyer_receive_distinct_review_languages(monkeypatch) -> None:
    @asynccontextmanager
    async def mock_db(*_args, **_kwargs):
        yield object()

    @asynccontextmanager
    async def english_buyer(_factory, telegram_id):
        assert telegram_id == 300
        token = _language.set("en")
        try:
            yield
        finally:
            _language.reset(token)

    item = _item()
    result = ReviewResult(item=item, changed=True)
    monkeypatch.setattr(commerce, "system_project_session_scope", mock_db)
    monkeypatch.setattr(
        commerce, "review_payment_request_from_telegram",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(commerce, "recipient_language", english_buyer)
    sent = AsyncMock()
    monkeypatch.setattr(commerce, "_safe_send_message", sent)
    router = commerce.create_telegram_commerce_router(None, project_id=uuid4())
    handler = next(x.callback for x in router.callback_query.handlers
                   if x.callback.__name__ == "approve_handler")
    callback = SimpleNamespace(
        data="approve:" + encode_uuid(item.request_id),
        from_user=SimpleNamespace(id=200, username="owner", first_name="Owner"),
        message=None,
        answer=AsyncMock(),
    )
    bot = MagicMock()
    token = _language.set("ru")
    try:
        await handler(callback, bot)
        admin, customer = sent.await_args_list
        assert admin.args[1] == 200
        assert "Оплата подтверждена" in admin.args[2]
        assert customer.args[1] == 300
        assert "Subscription activated" in customer.args[2]
        assert localization.current_language() == "ru"
    finally:
        _language.reset(token)


async def test_access_invitation_uses_receivers_language(monkeypatch) -> None:
    @asynccontextmanager
    async def english_recipient(_factory, telegram_id):
        assert telegram_id == 300
        token = _language.set("en")
        try:
            yield
        finally:
            _language.reset(token)

    from telegram_membership_saas.modules.telegram_access import adapter as access_module

    monkeypatch.setattr(access_module, "recipient_language", english_recipient)
    bot = SimpleNamespace(send_message=AsyncMock())
    adapter = TelegramAccessAdapter(bot, session_factory=object())
    command = SimpleNamespace(telegram_user_id=300)
    invite = TelegramAccessResult(invite_link="https://t.me/+example")
    await adapter.deliver(command, invite)
    msg = bot.send_message.await_args.kwargs
    assert "Your channel access is ready" in msg["text"]
    assert msg["reply_markup"].inline_keyboard[0][0].text == "Join channel"
    assert msg["reply_markup"].inline_keyboard[0][0].url == invite.invite_link
