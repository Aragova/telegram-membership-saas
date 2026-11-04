"""Premium Telegram-native buyer storefront: currency → offer → eligible payment.

The existing checkout service is the source of truth for availability and
revalidates the chosen price/method in a transaction before creating a request.
No base currency, FX conversion, hidden static providers, or extra payment state.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.plans import PlanResource
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
    format_amount,
    format_period,
    list_checkout_options,
)

PAGE_SIZE = 7
CURRENCY_ORDER = ("RUB", "USD", "EUR", "BYN", "KZT", "AED", "USDT", "TON")


@dataclass(frozen=True, slots=True)
class BuyerOffer:
    option: CheckoutOption
    methods: tuple[CheckoutOption, ...]
    resource_label: str


def build_buyer_offers(
    options: tuple[CheckoutOption, ...],
    resources: dict[UUID, tuple[str, ...]] | None = None,
) -> tuple[BuyerOffer, ...]:
    """Collapse the SQL price×method rows to one offer per priced plan."""
    resources = resources or {}
    methods_by_price: dict[UUID, list[CheckoutOption]] = {}
    first: dict[UUID, CheckoutOption] = {}
    for option in options:
        first.setdefault(option.plan_price_id, option)
        methods = methods_by_price.setdefault(option.plan_price_id, [])
        if all(
            existing.payment_method_id != option.payment_method_id
            for existing in methods
        ):
            methods.append(option)
    result = []
    for price_id, option in first.items():
        names = resources.get(option.plan_id, ())
        label = (
            names[0] if len(names) == 1
            else " · ".join(names[:2]) + (" …" if len(names) > 2 else "")
            if names
            else "Подписки"
        )
        result.append(BuyerOffer(
            option=option,
            methods=tuple(methods_by_price[price_id]),
            resource_label=label,
        ))
    return tuple(result)


async def list_buyer_offers(
    session: AsyncSession, *, project_id: UUID,
) -> tuple[BuyerOffer, ...]:
    """Use only checkout-eligible offers and current resource memberships."""
    options = await list_checkout_options(session, project_id=project_id)
    if not options:
        return ()
    plan_ids = tuple({item.plan_id for item in options})
    pairs = (await session.execute(
        select(PlanResource.plan_id, Resource.name)
        .join(
            Resource,
            and_(
                Resource.id == PlanResource.resource_id,
                Resource.project_id == PlanResource.project_id,
            ),
        )
        .where(
            PlanResource.project_id == project_id,
            PlanResource.plan_id.in_(plan_ids),
            Resource.status == ResourceStatus.ACTIVE.value,
        )
        .order_by(Resource.name)
    )).all()
    by_plan: dict[UUID, list[str]] = {}
    for plan_id, name in pairs:
        names = by_plan.setdefault(plan_id, [])
        if name not in names:
            names.append(name)
    return build_buyer_offers(
        options, {plan_id: tuple(names) for plan_id, names in by_plan.items()},
    )


def available_currencies(offers: tuple[BuyerOffer, ...]) -> tuple[str, ...]:
    codes = {o.option.currency_code for o in offers if o.methods}
    return tuple(sorted(
        codes, key=lambda code: (
            CURRENCY_ORDER.index(code) if code in CURRENCY_ORDER else 99,
            code,
        ),
    ))


def _currency_rows(
    codes: tuple[str, ...], current: str, *, plan_id: UUID | None = None,
    page: int = 0, offers: tuple[BuyerOffer, ...] = (),
) -> list[list[InlineKeyboardButton]]:
    buttons = []
    for code in codes:
        if plan_id is not None:
            matching = next(
                (offer for offer in offers
                 if offer.option.plan_id == plan_id
                 and offer.option.currency_code == code),
                None,
            )
            if matching is None:
                continue
            callback = f"offer:{encode_uuid(matching.option.plan_price_id)}:{page}"
        else:
            callback = f"cat:{code}:0"
        buttons.append(InlineKeyboardButton(
            text=("✓ " if code == current else "") + code,
            callback_data=callback,
        ))
    return [buttons[i:i + 4] for i in range(0, len(buttons), 4)]


def catalog_screen(
    offers: tuple[BuyerOffer, ...], *, currency: str | None = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    currencies = available_currencies(offers)
    if not currencies:
        return (
            "✦ <b>Подписки</b>\n\n"
            "Пока нет тарифов, доступных для покупки. Загляните позже.",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )
    chosen = currency if currency in currencies else currencies[0]
    selected = [o for o in offers if o.option.currency_code == chosen]
    total_pages = max(1, (len(selected) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = max(0, min(page, total_pages - 1))
    visible = selected[
        current_page * PAGE_SIZE:(current_page + 1) * PAGE_SIZE
    ]
    rows = _currency_rows(currencies, chosen)
    lines = [
        "✦ <b>Подписки</b>",
        "Выберите срок и тариф.",
        "",
        "Валюта: <b>" + escape(chosen) + "</b>",
    ]
    current_resource = None
    for item in visible:
        option = item.option
        if item.resource_label != current_resource:
            current_resource = item.resource_label
            lines.extend(["", "📢 <b>" + escape(current_resource) + "</b>"])
        price = format_amount(option.amount, option.currency_precision)
        period = format_period(option.duration_value, option.duration_unit)
        lines.append(
            "• " + escape(option.plan_name) + " · " + escape(period)
            + " — <b>" + escape(price + " " + chosen) + "</b>"
        )
        rows.append([InlineKeyboardButton(
            text=(
                "💎 " + option.plan_name[:23] + " · " + period
                + " · " + price + " " + chosen
            )[:64],
            callback_data=(
                f"offer:{encode_uuid(option.plan_price_id)}:{current_page}"
            ),
        )])
    if total_pages > 1:
        nav = []
        if current_page > 0:
            nav.append(InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"cat:{chosen}:{current_page - 1}",
            ))
        if current_page + 1 < total_pages:
            nav.append(InlineKeyboardButton(
                text="Далее ➡️",
                callback_data=f"cat:{chosen}:{current_page + 1}",
            ))
        rows.append(nav)
        lines.extend(["", f"Страница {current_page + 1} из {total_pages}"])
    lines.extend(["", "Способ оплаты выберете на следующем шаге."])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def offer_screen(
    offers: tuple[BuyerOffer, ...], *, price_id: UUID, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup] | None:
    current = next(
        (offer for offer in offers if offer.option.plan_price_id == price_id),
        None,
    )
    if current is None:
        return None
    option = current.option
    codes = available_currencies(tuple(
        offer for offer in offers if offer.option.plan_id == option.plan_id
    ))
    rows = _currency_rows(
        codes, option.currency_code, plan_id=option.plan_id,
        page=page, offers=offers,
    )
    price = format_amount(option.amount, option.currency_precision)
    period = format_period(option.duration_value, option.duration_unit)
    text = (
        "✦ <b>Оформление подписки</b>\n\n"
        "📢 " + escape(current.resource_label) + "\n"
        "💎 <b>" + escape(option.plan_name) + "</b>\n"
        "⏳ " + escape(period) + "\n"
        "💰 <b>" + escape(price + " " + option.currency_code) + "</b>\n\n"
        "Выберите удобный способ оплаты:"
    )
    for method in current.methods:
        rows.append([InlineKeyboardButton(
            text="💳 " + method.payment_method_name[:55],
            callback_data=(
                f"buy:{encode_uuid(option.plan_price_id)}:"
                f"{encode_uuid(method.payment_method_id)}"
            ),
        )])
    rows.append([InlineKeyboardButton(
        text="⬅️ Все тарифы",
        callback_data=f"cat:{option.currency_code}:{page}",
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
