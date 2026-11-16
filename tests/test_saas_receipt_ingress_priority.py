"""Regression: real ViMi Pay receipt replies must precede all tenant editors.

No Telegram API traffic, financial confirmation or new UI controls.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.types import (
    Message,
    RichBlockParagraph,
    RichBlockSectionHeading,
    RichMessage,
    RichTextBold,
    RichTextCode,
)

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
        document=SimpleNamespace(file_id="fake-pdf-id", file_size=1024) if content == "document"
                 else None,
        photo=[SimpleNamespace(file_id="fake-photo-id", file_size=1024)]
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


@pytest.mark.asyncio
async def test_receipt_ack_does_not_push_from_foreign_customer_bot(monkeypatch):
    message, _ = _receipt()
    monkeypatch.setattr(
        ingress, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ingress, "saas_v2_submit_proof", AsyncMock())
    bot = SimpleNamespace(
        id=123, send_message=AsyncMock(), send_document=AsyncMock(),
        send_photo=AsyncMock(),
    )

    await ingress.create_saas_receipt_router(
        object(),
    ).message.handlers[0].callback(message, bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == message.chat.id
    bot.send_document.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


@pytest.mark.parametrize("first_block", ["paragraph", "heading"])
@pytest.mark.asyncio
async def test_old_rich_invoice_receipt_pdf_registers_in_existing_queue(
    monkeypatch, first_block,
):
    """Telegram 10.1 Rich messages have rich_message.blocks, not .text."""
    message, attempt = _receipt()
    original = message.reply_to_message
    original.text = None
    original.caption = None
    content = [
        "🧾 ViMi Pay · ", RichTextCode(text=str(attempt)),
        "\n\n💳 ViMi Pay · RUB",
    ]
    block = (
        RichBlockParagraph(text=content)
        if first_block == "paragraph" else
        RichBlockSectionHeading(size=3, text=content)
    )
    original.rich_message = RichMessage(blocks=[block])
    registered = AsyncMock()
    monkeypatch.setattr(
        ingress, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ingress, "saas_v2_submit_proof", registered)
    bot = SimpleNamespace(id=123, send_message=AsyncMock())
    router = ingress.create_saas_receipt_router(object(), project_id=uuid4())
    assert ingress.receipt_attempt_id(message, bot.id) == attempt
    assert await router.message.handlers[0].filters[0].callback(
        message, bot,
    )
    await router.message.handlers[0].callback(message, bot)
    registered.assert_awaited_once()
    assert registered.await_args.kwargs["attempt_id"] == attempt
    assert registered.await_args.kwargs["file_type"] == "document"
    assert "Чек зарегистрирован" in bot.send_message.await_args.args[1]


def test_clean_rich_payment_prompt_with_footer_reference():
    message, attempt = _receipt()
    prompt = message.reply_to_message
    prompt.text = None
    prompt.caption = None
    prompt.rich_message = RichMessage(blocks=[
        RichBlockParagraph(text="🧾 ViMi Pay · Оплата счёта"),
        RichBlockParagraph(text="💳 Оплата по реквизитам"),
        RichBlockParagraph(text=[
            "🔐 Код заявки: ", RichTextCode(text=str(attempt)),
        ]),
    ])
    assert ingress.receipt_attempt_id(message, 123) == attempt


def test_rich_invoice_reply_parses_from_actual_aiogram_message_payload():
    """Use the Bot API payload shape, not mocked Message.text shortcuts."""
    invoice = uuid4()
    header = "🧾 ViMi Pay · "
    user = {"id": 456, "is_bot": False, "first_name": "Merchant"}
    source_bot = {"id": 123, "is_bot": True, "first_name": "ViMi"}
    chat = {"id": 456, "type": "private", "first_name": "Merchant"}
    receipt = Message.model_validate({
        "message_id": 201,
        "date": 1794620000,
        "chat": chat,
        "from": user,
        "document": {
            "file_id": "fake-document", "file_unique_id": "unique-document",
        },
        "reply_to_message": {
            "message_id": 200,
            "date": 1794619900,
            "chat": chat,
            "from": source_bot,
            "rich_message": {
                "blocks": [{
                    "type": "paragraph",
                    "text": [header, {
                        "type": "code", "text": str(invoice),
                    }, "\n\n💳 RUB"],
                }],
            },
        },
    })
    assert receipt.reply_to_message.text is None
    assert receipt.reply_to_message.rich_message is not None
    assert ingress.receipt_attempt_id(receipt, source_bot["id"]) == invoice
    assert ingress.receipt_attempt_id(receipt, 111) is None


def test_rich_receipt_rejects_unrelated_blocks_and_deceptive_content():
    message, attempt = _receipt()
    prompt = message.reply_to_message
    prompt.text = None
    prompt.caption = None
    prompt.rich_message = RichMessage(blocks=[
        RichBlockParagraph(text=[
            "📂 File Sales · ", RichTextCode(text=str(attempt)),
        ]),
    ])
    assert ingress.receipt_attempt_id(message, 123) is None
    prompt.rich_message = RichMessage(blocks=[
        RichBlockParagraph(text=RichTextBold(text="🧾 ViMi Pay · invalid")),
    ])
    assert ingress.receipt_attempt_id(message, 123) is None
    prompt.rich_message = RichMessage(blocks=[
        RichBlockParagraph(text=[
            "🧾 ViMi Pay · ", RichTextCode(text=str(attempt)),
        ]),
    ])
    prompt.from_user.id = 999
    assert ingress.receipt_attempt_id(message, 123) is None


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


@pytest.mark.parametrize("content,too_large", [
    ("document", 20 * 1024 * 1024 + 1),
    ("photo", 10 * 1024 * 1024 + 1),
])
async def test_oversized_platform_receipt_rejected_before_registration(
    monkeypatch, content, too_large,
):
    message, _ = _receipt(content=content)
    target = message.document if content == "document" else message.photo[-1]
    target.file_size = too_large
    record = AsyncMock()
    actor = AsyncMock()
    monkeypatch.setattr(ingress, "saas_v2_submit_proof", record)
    monkeypatch.setattr(ingress, "ensure_telegram_actor", actor)
    bot = SimpleNamespace(id=123, send_message=AsyncMock())
    await ingress.create_saas_receipt_router(
        object(),
    ).message.handlers[0].callback(message, bot)
    record.assert_not_awaited()
    actor.assert_not_awaited()
    assert "не зарегистрирован" in bot.send_message.await_args.args[1]


@pytest.mark.parametrize("size", [None, 0])
async def test_unknown_platform_file_size_is_verified_or_rejected(
    monkeypatch, size,
):
    message, _ = _receipt()
    message.document.file_size = size
    record = AsyncMock()
    monkeypatch.setattr(ingress, "saas_v2_submit_proof", record)
    monkeypatch.setattr(ingress, "ensure_telegram_actor", AsyncMock())
    bot = SimpleNamespace(
        id=123, send_message=AsyncMock(),
        get_file=AsyncMock(return_value=SimpleNamespace(file_size=None)),
    )
    await ingress.create_saas_receipt_router(
        object(),
    ).message.handlers[0].callback(message, bot)
    record.assert_not_awaited()
    assert "не создана" in bot.send_message.await_args.args[1]


def test_clean_payment_prompt_spoiler_reference_and_legacy_replies():
    message, attempt = _receipt()
    # Still accepts already-issued Rich/HTML invoice payment messages.
    assert ingress.receipt_attempt_id(message, 123) == attempt
    message.reply_to_message.text = (
        "🧾 ViMi Pay · Оплата счёта\n\n"
        "💳 СБП\n150.00 RUB\n"
        "После перевода ответьте на это сообщение чеком.\n\n"
        "🔐 Код заявки: " + str(attempt)
    )
    assert ingress.receipt_attempt_id(message, 123) == attempt
    message.reply_to_message.text = (
        "🧾 ViMi Pay · Invoice payment\n\n"
        "150 RUB\n\n🔐 Reference: " + str(attempt)
    )
    assert ingress.receipt_attempt_id(message, 123) == attempt
    # Malformed / missing tail marker cannot register a guessed attempt.
    message.reply_to_message.text += " extra"
    assert ingress.receipt_attempt_id(message, 123) is None
    message.reply_to_message.from_user.id = 555
    assert ingress.receipt_attempt_id(message, 123) is None
