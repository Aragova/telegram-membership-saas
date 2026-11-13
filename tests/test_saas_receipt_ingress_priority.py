"""Regression: real ViMi Pay receipt replies must precede all tenant editors.

No Telegram API traffic, financial confirmation or new UI controls.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_receipt_handlers as ingress
from telegram_membership_saas.telegram_adapter import (
    create_customer_dispatcher,
    create_dispatcher,
)


def _receipt(*, source_bot=123, user=456, content="document",
             attempt=None, prompt_text=None):
    attempt = attempt or uuid4()
    prompt = SimpleNamespace(
        from_user=SimpleNamespace(id=source_bot),
        text=prompt_text or "🧾 ViMi Pay · " + str(attempt) + "\n\n"
             + "💳 ViMi Pay · RUB\n150.00 RUB",
        caption=None,
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user, username="merchant", first_name="Merchant",
        ),
        chat=SimpleNamespace(id=user, type="private"),
        reply_to_message=prompt,
        document=SimpleNamespace(file_id="fake-pdf-id") if content == "document"
                 else None,
        photo=[SimpleNamespace(file_id="fake-photo-id")]
              if content == "photo" else None,
        message_id=177,
    ), attempt


def test_saas_receipt_precedes_tenant_file_editor_and_file_checkout():
    dispatcher = create_customer_dispatcher(object(), project_id=uuid4())
    names = [router.name for router in dispatcher.sub_routers]
    assert names.index("vimi-saas-proof-ingress") < names.index(
        "digital-file-sales"
    )
    assert names.index("vimi-saas-proof-ingress") < names.index(
        "manual-file-checkout"
    )
    assert names.count("vimi-saas-proof-ingress") == 1


def test_saas_receipt_precedes_central_platform_forms(monkeypatch):
    monkeypatch.setattr(
        "telegram_membership_saas.telegram_adapter.get_settings",
        lambda: SimpleNamespace(platform_owner_telegram_id=999),
    )
    dispatcher = create_dispatcher(object())
    names = [router.name for router in dispatcher.sub_routers]
    assert names.index("vimi-saas-proof-ingress") < names.index(
        "central-registrar"
    )
    assert names.count("vimi-saas-proof-ingress") == 1


@pytest.mark.parametrize("content", ["photo", "document"])
@pytest.mark.asyncio
async def test_platform_receipt_from_real_bot_prompt_registers_and_responds(
    monkeypatch, content,
):
    message, attempt = _receipt(content=content)
    actor = uuid4()
    register = AsyncMock()
    monkeypatch.setattr(ingress, "ensure_telegram_actor",
                        AsyncMock(return_value=actor))
    monkeypatch.setattr(ingress, "saas_v2_submit_proof", register)
    monkeypatch.setattr(
        ingress, "get_settings",
        lambda: SimpleNamespace(platform_owner_telegram_id=None),
    )
    bot = SimpleNamespace(id=123, send_message=AsyncMock())
    router = ingress.create_saas_receipt_router(
        object(), project_id=uuid4(),
    )
    assert await router.message.handlers[0].filters[0].callback(
        message, bot,
    )
    await router.message.handlers[0].callback(message, bot)
    assert register.await_count == 1
    kwargs = register.await_args.kwargs
    assert kwargs["attempt_id"] == attempt
    assert kwargs["bot_id"] == bot.id
    assert kwargs["chat_id"] == message.from_user.id
    assert kwargs["file_type"] == content
    assert kwargs["file_id"] == (
        "fake-pdf-id" if content == "document" else "fake-photo-id"
    )
    bot.send_message.assert_awaited_once()
    assert "Чек зарегистрирован" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_failed_receipt_registration_yields_visible_error(monkeypatch):
    message, _ = _receipt()
    monkeypatch.setattr(
        ingress, "ensure_telegram_actor",
        AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ingress, "saas_v2_submit_proof",
        AsyncMock(side_effect=ValueError("wrong invoice")),
    )
    bot = SimpleNamespace(id=123, send_message=AsyncMock())
    router = ingress.create_saas_receipt_router(object())
    await router.message.handlers[0].callback(message, bot)
    bot.send_message.assert_awaited_once()
    assert "не относится" in bot.send_message.await_args.args[1]


def test_receipt_filter_rejects_foreign_bot_spoofed_or_unrelated_documents():
    authentic, uid = _receipt()
    assert ingress.receipt_attempt_id(authentic, 123) == uid
    authentic.reply_to_message.from_user.id = 999
    assert ingress.receipt_attempt_id(authentic, 123) is None
    authentic.reply_to_message.from_user.id = 123
    authentic.reply_to_message.text = "🧾 Not ViMi Pay · " + str(uid)
    assert ingress.receipt_attempt_id(authentic, 123) is None
    authentic.reply_to_message.text = "🧾 ViMi Pay · "
    assert ingress.receipt_attempt_id(authentic, 123) is None
    authentic.reply_to_message.text = "🧾 ViMi Pay · invalid"
    assert ingress.receipt_attempt_id(authentic, 123) is None
    authentic.reply_to_message.text = "🧾 ViMi Pay · " + uid.hex
    assert ingress.receipt_attempt_id(authentic, 123) == uid
    authentic.chat.id = 1000
    assert ingress.receipt_attempt_id(authentic, 123) is None
