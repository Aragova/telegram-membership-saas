"""Rich subscription offer pilot: bilingual visuals, isolation and safe callbacks."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from telegram_membership_saas import localization
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.buyer_preview import (
    preview_offer_screen,
)
from telegram_membership_saas.modules.telegram_commerce import handlers as buyer_ui
from telegram_membership_saas.modules.telegram_commerce import rich_ui
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    BuyerOffer,
    offer_screen,
)


def _offer(*, name="Premium", months=1, unit="month", price="500.00"):
    option = CheckoutOption(
        plan_id=uuid4(),
        plan_name=name,
        duration_value=months,
        duration_unit=unit,
        plan_price_id=uuid4(),
        currency_code="RUB",
        amount=Decimal(price),
        currency_precision=2,
        payment_method_id=uuid4(),
        payment_method_name="Банк & Co",
    )
    return BuyerOffer(
        option=option, methods=(option,),
        resource_label="ONLY <YOU> & FULL",
    )


def _bot(bot_id=8528484991):
    bot = MagicMock()
    bot.id = bot_id
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _callbacks(markup):
    return [
        button.callback_data for row in markup.inline_keyboard
        for button in row
    ]


@pytest.mark.parametrize(
    ("language", "labels", "prompt"),
    [
        ("ru", ("Канал", "Тариф", "Срок", "Цена"),
         "Как вам удобнее оплатить?"),
        ("en", ("Channel", "Plan", "Duration", "Price"),
         "How would you like to pay?"),
    ],
)
def test_rich_subscription_offer_named_price_locale_and_escaped_values(
    language, labels, prompt,
):
    offer = _offer(name="Premium <VIP> & Friends", price="100.2500")
    token = localization._language.set(language)
    try:
        html = rich_ui.rich_subscription_offer_html(offer)
    finally:
        localization._language.reset(token)
    for label in labels:
        assert label in html
    assert prompt in html
    assert "ONLY &lt;YOU&gt; &amp; FULL" in html
    assert "Premium &lt;VIP&gt; &amp; Friends" in html
    assert "&lt;VIP&gt;" in html and "<VIP>" not in html
    assert "<table bordered compact>" in html
    assert "<td>100.25 RUB</td>" in html
    assert html.count("<tr>") == 4
    assert html.index(labels[0]) < html.index(labels[1])
    assert html.index(labels[1]) < html.index(labels[2])
    assert html.index(labels[2]) < html.index(labels[3])


@pytest.mark.parametrize(
    ("language", "period"),
    [("ru", "1 месяц"), ("en", "1 month")],
)
def test_duration_only_plan_does_not_create_fake_plan_name(language, period):
    offer = _offer(name="1 месяц")
    token = localization._language.set(language)
    try:
        html = rich_ui.rich_subscription_offer_html(offer, preview=True)
    finally:
        localization._language.reset(token)
    assert "<tr>" in html and html.count("<tr>") == 3
    assert period in html
    assert "<th>🎟" not in html
    assert "Предпросмотр" in html if language == "ru" else "preview" in html
    assert "Как вам удобнее оплатить" not in html
    assert "How would you like to pay" not in html


def test_lifetime_period_is_localized_and_not_repeated():
    offer = _offer(name="Навсегда", months=None, unit="lifetime", price="1200")
    token = localization._language.set("en")
    try:
        html = rich_ui.rich_subscription_offer_html(offer)
    finally:
        localization._language.reset(token)
    assert html.count("Lifetime") == 1
    assert "1200 RUB" in html
    assert "Навсегда" not in html


async def test_explicit_opt_in_only_and_same_keyboard(monkeypatch):
    monkeypatch.setattr(
        rich_ui, "get_settings",
        lambda: SimpleNamespace(rich_subscription_checkout_bot_ids="8528484991"),
    )
    offer = _offer()
    _, keyboard = offer_screen((offer,), price_id=offer.option.plan_price_id)
    enabled = _bot()
    assert await rich_ui.send_rich_subscription_offer(
        enabled, chat_id=100, rich_html="<h3>Rich</h3>",
        reply_markup=keyboard,
    )
    enabled.send_message.assert_not_awaited()
    sent = enabled.send_rich_message.await_args.kwargs
    assert sent["rich_message"].html == "<h3>Rich</h3>"
    assert sent["reply_markup"] is keyboard
    callbacks = _callbacks(sent["reply_markup"])
    assert any(x.startswith("buy:") for x in callbacks)
    assert any(x.startswith("offer:") for x in callbacks)
    assert any(x.startswith("cat:") for x in callbacks)
    disabled = _bot(11111)
    assert not await rich_ui.send_rich_subscription_offer(
        disabled, chat_id=100, rich_html="<h3>Rich</h3>",
        reply_markup=keyboard,
    )
    disabled.send_rich_message.assert_not_awaited()
    assert await rich_ui.send_rich_subscription_offer(
        disabled, chat_id=100, rich_html="<h3>Preview</h3>",
        reply_markup=keyboard, preview=True,
    )


async def test_bad_rich_format_falls_back_without_transport_retry(monkeypatch):
    monkeypatch.setattr(
        rich_ui, "get_settings",
        lambda: SimpleNamespace(rich_subscription_checkout_bot_ids="8528484991"),
    )
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=100, text="dummy"),
        message="Bad Request: can't parse Rich HTML",
    )
    offer = _offer()
    _, keyboard = offer_screen((offer,), price_id=offer.option.plan_price_id)
    assert not await rich_ui.send_rich_subscription_offer(
        bot, chat_id=100, rich_html="<h3>Rich</h3>",
        reply_markup=keyboard,
    )
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_buyer_offer_screen_rich_and_fallback_use_same_legacy_keyboard(
    monkeypatch,
):
    offer = _offer()
    options = (offer,)
    @asynccontextmanager
    async def scope(*_a, **_k):
        yield MagicMock()

    monkeypatch.setattr(buyer_ui, "system_project_session_scope", scope)
    monkeypatch.setattr(
        buyer_ui, "list_buyer_offers", AsyncMock(return_value=options),
    )
    monkeypatch.setattr(buyer_ui, "rich_subscription_enabled", lambda _id: True)
    rich_send = AsyncMock(return_value=True)
    screen = AsyncMock()
    monkeypatch.setattr(buyer_ui, "send_rich_subscription_offer", rich_send)
    monkeypatch.setattr(buyer_ui, "_send_screen", screen)
    bot = _bot()
    args = dict(
        bot=bot, chat_id=100, session_factory=MagicMock(), project_id=uuid4(),
        price_id=offer.option.plan_price_id, message_id=77,
    )
    result = await buyer_ui._send_catalog(**args)
    assert result is True
    rich_send.assert_awaited_once()
    screen.assert_not_awaited()
    _, legacy_keyboard = offer_screen(
        options, price_id=offer.option.plan_price_id,
    )
    assert (
        _callbacks(rich_send.await_args.kwargs["reply_markup"])
        == _callbacks(legacy_keyboard)
    )

    rich_send.reset_mock()
    rich_send.return_value = False
    await buyer_ui._send_catalog(**args)
    screen.assert_awaited_once()
    assert screen.await_args.kwargs["message_id"] == 77
    assert _callbacks(screen.await_args.kwargs["keyboard"]) == _callbacks(
        legacy_keyboard,
    )

    # Stale price never reaches either Rich sender or invoice creation.
    rich_send.reset_mock()
    screen.reset_mock()
    result = await buyer_ui._send_catalog(
        **{**args, "price_id": uuid4()},
    )
    assert result is False
    rich_send.assert_not_awaited()
    screen.assert_not_awaited()


async def test_owner_rich_preview_is_read_only_and_permission_checked(monkeypatch):
    offer = _offer(name="1 месяц")
    project, owner = uuid4(), uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=owner),
    )
    can_manage = AsyncMock(return_value=True)
    monkeypatch.setattr(owner_ui, "can_manage_project", can_manage)
    monkeypatch.setattr(
        owner_ui, "get_selected_project", AsyncMock(return_value=project),
    )
    monkeypatch.setattr(
        owner_ui, "list_buyer_offers", AsyncMock(return_value=(offer,)),
    )
    session = MagicMock()
    session.scalar = AsyncMock()
    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(owner_ui, "system_project_session_scope", scope)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(owner_ui, "send_rich_subscription_offer", send)
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    cb = next(h.callback for h in router.callback_query.handlers
              if h.callback.__name__ == "owner_callback")
    query = SimpleNamespace(
        data="own:pvrich:" + encode_uuid(offer.option.plan_price_id) + ":0",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=100), message_id=77,
        ),
        from_user=SimpleNamespace(
            id=100, first_name="Owner", username="owner",
        ),
        answer=AsyncMock(),
    )
    bot = _bot()
    await cb(query, bot)
    send.assert_awaited_once()
    sent = send.await_args.kwargs
    assert sent["preview"] is True
    assert "payment" not in sent["rich_html"].lower()
    callbacks = _callbacks(sent["reply_markup"])
    assert "own:dashboard" in callbacks
    assert all(not x.startswith(("buy:", "paid:", "cancel:"))
               for x in callbacks)
    assert "own:pvmethod:" + encode_uuid(offer.option.plan_price_id) in (
        " ".join(callbacks)
    )
    session.add.assert_not_called()
    session.flush.assert_not_called()

    can_manage.return_value = False
    await cb(query, bot)
    send.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


def test_owner_offer_preview_includes_rich_action_without_unsafe_callbacks():
    offer = _offer()
    text, keyboard = preview_offer_screen(
        (offer,), price_id=offer.option.plan_price_id,
    )
    assert "Предпросмотр" in text
    callbacks = _callbacks(keyboard)
    assert (
        "own:pvrich:" + encode_uuid(offer.option.plan_price_id) + ":0"
    ) in callbacks
    assert all(not x.startswith(("buy:", "offer:", "cat:")) for x in callbacks)


@pytest.mark.parametrize(("language", "notice_text"), [
    ("ru", "❌ Заявка отменена."),
    ("en", "❌ Request canceled."),
])
async def test_cancellation_notice_preserves_rich_subscription_catalog(
    monkeypatch, language, notice_text,
):
    offer = _offer(name="1 месяц", price="500")
    @asynccontextmanager
    async def scope(*_a, **_k):
        yield MagicMock()

    monkeypatch.setattr(buyer_ui, "system_project_session_scope", scope)
    monkeypatch.setattr(
        buyer_ui, "list_buyer_offers", AsyncMock(return_value=(offer,)),
    )
    monkeypatch.setattr(buyer_ui, "rich_subscription_enabled", lambda _id: True)
    rich_send = AsyncMock(return_value=True)
    fallback = AsyncMock()
    monkeypatch.setattr(buyer_ui, "send_rich_subscription_offer", rich_send)
    monkeypatch.setattr(buyer_ui, "_send_screen", fallback)
    bot = _bot()
    token = localization._language.set(language)
    try:
        result = await buyer_ui._send_catalog(
            bot, chat_id=100, session_factory=MagicMock(),
            project_id=uuid4(), notice=notice_text,
        )
    finally:
        localization._language.reset(token)
    assert result is True
    rich_send.assert_awaited_once()
    fallback.assert_not_awaited()
    kwargs = rich_send.await_args.kwargs
    html = kwargs["rich_html"]
    assert "<blockquote><b>" + notice_text + "</b></blockquote>" in html
    assert "<table bordered compact>" in html
    assert "500 RUB" in html
    assert kwargs["message_id"] is None
    _, legacy_keyboard = buyer_ui.catalog_screen((offer,))
    assert _callbacks(kwargs["reply_markup"]) == _callbacks(legacy_keyboard)


def test_rich_catalog_cancellation_notice_is_html_escaped():
    offer = _offer()
    html = buyer_ui.rich_catalog_html(
        (offer,), notice="❌ Отменено <script> & html",
    )
    assert "<blockquote><b>❌ Отменено &lt;script&gt; &amp; html</b></blockquote>" in html
    assert "<script>" not in html
    assert "<table bordered compact>" in html


async def test_cancellation_notice_classic_fallback_is_unchanged(monkeypatch):
    offer = _offer(name="1 месяц", price="500")
    @asynccontextmanager
    async def scope(*_a, **_k):
        yield MagicMock()

    monkeypatch.setattr(buyer_ui, "system_project_session_scope", scope)
    monkeypatch.setattr(
        buyer_ui, "list_buyer_offers", AsyncMock(return_value=(offer,)),
    )
    monkeypatch.setattr(buyer_ui, "rich_subscription_enabled", lambda _id: True)
    rich_send = AsyncMock(return_value=False)
    screen = AsyncMock()
    monkeypatch.setattr(buyer_ui, "send_rich_subscription_offer", rich_send)
    monkeypatch.setattr(buyer_ui, "_send_screen", screen)
    await buyer_ui._send_catalog(
        _bot(), chat_id=100, session_factory=MagicMock(),
        project_id=uuid4(), notice="❌ Request canceled.",
    )
    rich_send.assert_awaited_once()
    screen.assert_awaited_once()
    assert "❌ Request canceled." in screen.await_args.kwargs["text"]
    _, legacy_keyboard = buyer_ui.catalog_screen((offer,))
    assert _callbacks(screen.await_args.kwargs["keyboard"]) == _callbacks(
        legacy_keyboard,
    )
