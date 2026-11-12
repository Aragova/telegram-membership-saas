"""Read-only buyer journey for connected bot administrators.

Render the real eligible buyer catalog and checkout choices. Every callback is
rewritten into an owner-only namespace so preview taps can never create a
PaymentRequest, receipt, subscription, or access grant.
"""
from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.currency_display import display_price
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.payments.service import render_manual_instruction
from telegram_membership_saas.modules.telegram_commerce.service import (
    encode_uuid,
    format_amount,
    format_period,
    is_period_only_plan_name,
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
    return (
        _preview_title() + text + _preview_disclaimer(),
        _preview_markup(keyboard, page=page),
    )


def _instruction_pages(instruction: str, *, max_escaped: int) -> tuple[str, ...]:
    """Split on characters BEFORE HTML escaping, never inside an entity."""
    parts = []
    current = []
    current_len = 0
    for character in instruction:
        encoded = escape(character)
        if current and current_len + len(encoded) > max_escaped:
            parts.append("".join(current))
            current = []
            current_len = 0
        current.append(encoded)
        current_len += len(encoded)
    if current:
        parts.append("".join(current))
    return tuple(parts) or ("",)


def preview_method_screen(
    offers: tuple[BuyerOffer, ...], *, price_id: UUID,
    payment_method_id: UUID, instruction_template: str,
    page: int = 0, instruction_page: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Display the full manual instruction in bounded, safe preview pages."""
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
        plan=period if is_period_only_plan_name(
            option.plan_name, option.duration_value, option.duration_unit,
        ) else option.plan_name,
        period=period,
    )
    heading = (
        _preview_title()
        + tr("💳 <b>Инструкция по оплате</b>\n\n",
             "💳 <b>Payment instructions</b>\n\n")
        + "📢 " + escape(offer.resource_label) + "\n"
        + (
            ""
            if is_period_only_plan_name(
                option.plan_name, option.duration_value, option.duration_unit,
            )
            else "💎 <b>" + escape(option.plan_name) + "</b>\n"
        )
        + "⏳ " + escape(period) + "\n"
        + "💰 <b>" + escape(display_price(amount, option.currency_code)) + "</b>\n"
        + tr("Способ: ", "Method: ")
        + escape(selected.payment_method_name) + "\n\n"
    )
    footer = _preview_disclaimer()
    # Telegram limits text to 4096 parsed characters. Count the longer
    # HTML-escaped form, leaving room for page labels and Unicode characters.
    budget = min(2500, max(128, 3850 - len(heading) - len(footer) - 80))
    parts = _instruction_pages(instruction, max_escaped=budget)
    if not 0 <= instruction_page < len(parts):
        return None
    text = heading + parts[instruction_page]
    if len(parts) > 1:
        text += tr(
            f"\n\n<i>Инструкция · часть {instruction_page + 1}/{len(parts)}</i>",
            f"\n\n<i>Instructions · part {instruction_page + 1}/{len(parts)}</i>",
        )
    text += footer

    rows = []
    if len(parts) > 1:
        nav = []
        prefix = (
            f"own:pvi:{encode_uuid(price_id)}:"
            f"{encode_uuid(payment_method_id)}:{page}:"
        )
        if instruction_page > 0:
            nav.append(InlineKeyboardButton(
                text=tr("⬅️ Назад", "⬅️ Previous"),
                callback_data=prefix + str(instruction_page - 1),
            ))
        if instruction_page + 1 < len(parts):
            nav.append(InlineKeyboardButton(
                text=tr("Далее ➡️", "Next ➡️"),
                callback_data=prefix + str(instruction_page + 1),
            ))
        rows.append(nav)
    rows.extend([
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
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
