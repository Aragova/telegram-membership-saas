"""Read-only buyer journey for connected bot administrators.

Render the real eligible buyer catalog and checkout choices. Every callback is
rewritten into an owner-only namespace so preview taps can never create a
PaymentRequest, receipt, subscription, or access grant.
"""
from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.payments.service import render_manual_instruction
from telegram_membership_saas.modules.telegram_commerce.service import (
    encode_uuid,
    format_amount,
    format_period,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    BuyerOffer,
    catalog_screen,
    offer_screen,
)


def _preview_markup(
    keyboard: InlineKeyboardMarkup, *, page: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in keyboard.inline_keyboard:
        translated = []
        for button in row:
            original = button.callback_data or ""
            if original.startswith("cat:"):
                callback = "own:pvcat:" + original[4:]
            elif original.startswith("offer:"):
                callback = "own:pvoffer:" + original[6:]
            elif original.startswith("buy:"):
                callback = "own:pvmethod:" + original[4:] + ":" + str(page)
            else:
                # Keep the global language switch; never allow a real buy action.
                callback = original
            translated.append(button.model_copy(update={"callback_data": callback}))
        rows.append(translated)
    rows.append([InlineKeyboardButton(
        text=tr("⬅️ Панель администратора", "⬅️ Admin panel"),
        callback_data="own:dashboard",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preview_title() -> str:
    return tr("👀 <b>Предпросмотр для покупателя</b>\n\n",
              "👀 <b>Buyer preview</b>\n\n")


def _preview_disclaimer() -> str:
    return tr(
        "\n\n<i>Демонстрация. Заявка на оплату и доступ не создаются.</i>",
        "\n\n<i>Preview only. No payment request or access is created.</i>",
    )


def preview_catalog_screen(
    offers: tuple[BuyerOffer, ...], *, currency: str | None = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Exactly the buyer's eligible offers, priced by selected currency."""
    text, keyboard = catalog_screen(offers, currency=currency, page=page)
    return _preview_title() + text + _preview_disclaimer(), _preview_markup(
        keyboard,
    )


def preview_offer_screen(
    offers: tuple[BuyerOffer, ...], *, price_id: UUID, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Same currency and eligible payment-method choices; no real checkout."""
    rendered = offer_screen(offers, price_id=price_id, page=page)
    if rendered is None:
        return None
    text, keyboard = rendered
    return _preview_title() + text + _preview_disclaimer(), _preview_markup(
        keyboard, page=page,
    )


def preview_method_screen(
    offers: tuple[BuyerOffer, ...], *, price_id: UUID,
    payment_method_id: UUID, instruction_template: str,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Render configured manual instructions without making a payment request."""
    offer = next(
        (item for item in offers if item.option.plan_price_id == price_id),
        None,
    )
    if offer is None:
        return None
    selected = next(
        (item for item in offer.methods
         if item.payment_method_id == payment_method_id),
        None,
    )
    if selected is None:
        return None
    option = offer.option
    amount = format_amount(option.amount, option.currency_precision)
    period = format_period(option.duration_value, option.duration_unit)
    instruction = render_manual_instruction(
        instruction_template,
        amount=amount, currency=option.currency_code,
        plan=option.plan_name, period=period,
    )
    text = (
        _preview_title()
        + tr("💳 <b>Инструкция по оплате</b>\n\n",
             "💳 <b>Payment instructions</b>\n\n")
        + "📢 " + escape(offer.resource_label) + "\n"
        + "💎 <b>" + escape(option.plan_name) + "</b>\n"
        + "⏳ " + escape(period) + "\n"
        + "💰 <b>" + escape(amount + " " + option.currency_code) + "</b>\n"
        + tr("Способ: ", "Method: ")
        + escape(selected.payment_method_name) + "\n\n"
        + escape(instruction)
        + _preview_disclaimer()
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr("⬅️ К тарифу", "⬅️ Back to plan"),
            callback_data=f"own:pvoffer:{encode_uuid(price_id)}:{page}",
        )],
        [InlineKeyboardButton(
            text=tr("📦 Все тарифы", "📦 All plans"),
            callback_data=f"own:pvcat:{option.currency_code}:{page}",
        )],
        [InlineKeyboardButton(
            text=tr("⬅️ Панель администратора", "⬅️ Admin panel"),
            callback_data="own:dashboard",
        )],
    ])
