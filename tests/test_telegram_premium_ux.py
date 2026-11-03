from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.telegram_access import handlers as access_handlers
from telegram_membership_saas.modules.telegram_commerce import handlers
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    ReviewResult,
)


def _review_result(*, changed=True, period_end=None, username="buyer") -> ReviewResult:
    return ReviewResult(
        item=ReviewItem(
            request_id=uuid4(),
            customer_telegram_user_id=222,
            customer_username=username,
            plan_name="Premium <Gold>",
            amount=Decimal("10"),
            currency_code="RUB",
            payment_method_name="Manual",
            status="approved",
        ),
        changed=changed,
        subscription_period_end=period_end,
        project_timezone="Europe/Moscow",
    )


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _callback(request_id):
    return SimpleNamespace(
        data=f"approve:{handlers.encode_uuid(request_id)}",
        from_user=SimpleNamespace(id=111, username="owner", first_name="Owner"),
        answer=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace(id=111), message_id=1000),
    )


@asynccontextmanager
async def _scope(*_args, **_kwargs):
    yield object()


def _approve_handler():
    router = handlers.create_telegram_commerce_router(None, project_id=uuid4())
    return next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "approve_handler"
    )


def test_review_controls_are_colored() -> None:
    keyboard = handlers._review_keyboard(uuid4())
    ok, reject = keyboard.inline_keyboard[0]
    assert ok.style == "success"
    assert reject.style == "danger"


def test_checkout_controls_are_colored() -> None:
    keyboard = handlers._checkout_keyboard(uuid4())
    assert keyboard.inline_keyboard[0][0].style == "success"
    assert keyboard.inline_keyboard[1][0].style == "danger"


def test_payment_notifications_show_project_local_expiration_and_escaped_names() -> None:
    result = _review_result(
        username="seller<x>",
        period_end=datetime(2026, 12, 3, 17, 30, tzinfo=UTC),
    )
    admin = handlers._admin_review_result_text(result, approve=True)
    buyer = handlers._customer_review_result_text(result, approve=True)
    assert "03.12.2026 20:30" in admin
    assert "03.12.2026 20:30" in buyer
    assert "seller&lt;x&gt;" in admin
    assert "Premium &lt;Gold&gt;" in buyer
    assert "Если вы ещё не в канале" in buyer
    assert "в следующем сообщении" not in buyer
    assert "<x>" not in admin
    assert "активна" in admin


def test_lifetime_and_rejection_notifications_do_not_claim_subscription_started() -> None:
    lifetime = _review_result()
    assert "Бессрочно" in handlers._customer_review_result_text(lifetime, approve=True)
    rejected = handlers._admin_review_result_text(lifetime, approve=False)
    assert "Подписка не активирована" in rejected
    assert "активна до" not in rejected


async def test_admin_receives_durable_approval_and_buyer_sees_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _review_result(period_end=datetime(2026, 12, 3, 17, 30, tzinfo=UTC))
    monkeypatch.setattr(handlers, "system_project_session_scope", _scope)
    monkeypatch.setattr(
        handlers,
        "review_payment_request_from_telegram",
        AsyncMock(return_value=result),
    )
    callback = _callback(result.item.request_id)
    bot = _bot()

    await _approve_handler()(callback, bot)

    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=111,
        message_id=1000,
        reply_markup=None,
    )
    assert bot.send_message.await_count == 2
    admin = bot.send_message.await_args_list[0].kwargs
    buyer = bot.send_message.await_args_list[1].kwargs
    assert admin["chat_id"] == 111
    assert admin["parse_mode"] == "HTML"
    assert "Оплата подтверждена" in admin["text"]
    assert "03.12.2026 20:30" in admin["text"]
    assert buyer["chat_id"] == 222
    assert "Подписка активирована" in buyer["text"]


async def test_duplicate_approval_does_not_send_new_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _review_result(changed=False)
    monkeypatch.setattr(handlers, "system_project_session_scope", _scope)
    monkeypatch.setattr(
        handlers,
        "review_payment_request_from_telegram",
        AsyncMock(return_value=result),
    )
    callback = _callback(result.item.request_id)
    bot = _bot()

    await _approve_handler()(callback, bot)

    bot.send_message.assert_not_awaited()
    bot.edit_message_reply_markup.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_join_confirmation_is_visible_after_access_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telegram_membership_saas.modules.telegram_access import handlers as join

    project_id = uuid4()
    ticket_id = uuid4()
    invite = SimpleNamespace(name=project_id.hex, invite_link="https://t.me/+secure")
    request = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=222),
        invite_link=invite,
    )
    monkeypatch.setattr(
        join,
        "prepare_join_request_decision",
        AsyncMock(return_value=SimpleNamespace(approve=True, ticket_id=ticket_id)),
    )
    finalize = AsyncMock()
    monkeypatch.setattr(join, "finalize_join_request_approval", finalize)
    monkeypatch.setattr(join, "system_project_session_scope", _scope)
    bot = _bot()
    bot.approve_chat_join_request = AsyncMock()
    bot.revoke_chat_invite_link = AsyncMock()

    accepted = await access_handlers.process_chat_join_request(None, bot, request)

    assert accepted is True
    finalize.assert_awaited_once()
    bot.approve_chat_join_request.assert_awaited_once()
    assert "Вы в канале!" in bot.send_message.await_args.kwargs["text"]
