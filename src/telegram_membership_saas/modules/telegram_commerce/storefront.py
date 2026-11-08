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

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.plans import PlanResource
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
    format_amount,
    format_period,
    is_period_only_plan_name,
    list_checkout_options,
)

PAGE_SIZE = 7
# Keep the five most requested local currencies together in a single row.
# Telegram owns the pixel rendering; labels must remain compact on mobile.
CURRENCIES_PER_ROW = 5
CURRENCY_ORDER = (
    "RUB", "USD", "EUR", "UAH", "BYN", "KZT", "AED", "GBP",
    "CNY", "JPY", "PLN", "USDT", "TON",
)
CURRENCY_ICONS = {
    "RUB": "🇷🇺", "USD": "🇺🇸", "EUR": "🇪🇺", "UAH": "🇺🇦",
    "BYN": "🇧🇾", "KZT": "🇰🇿", "AED": "🇦🇪", "GBP": "🇬🇧",
    "CNY": "🇨🇳", "JPY": "🇯🇵", "PLN": "🇵🇱", "CHF": "🇨🇭",
    "CAD": "🇨🇦", "AUD": "🇦🇺", "TRY": "🇹🇷", "INR": "🇮🇳",
    "BRL": "🇧🇷", "KRW": "🇰🇷", "UZS": "🇺🇿", "GEL": "🇬🇪",
    "USDT": "💠", "TON": "💎", "BTC": "₿", "ETH": "◆",
}


def _currency_icon(code: str) -> str:
    return CURRENCY_ICONS.get(code, "🌐")


def _plan_summary(name: str, period: str, *, duration_only: bool) -> str:
    """Do not repeat the duration when the plan is unnamed."""
    return escape(period if duration_only else name + " · " + period)


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
            else tr("Подписки", "Subscriptions")
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
        selected = code == current
        # The filled green button and the emoji are both visible selections:
        # older Telegram clients may ignore button.style.
        label = ("🟢" if selected else "") + _currency_icon(code) + code
        buttons.append(InlineKeyboardButton(
            text=label,
            callback_data=callback,
            style="success" if selected else None,
        ))
    return [
        buttons[i:i + CURRENCIES_PER_ROW]
        for i in range(0, len(buttons), CURRENCIES_PER_ROW)
    ]


def buyer_root_screen(
    channel_name: str, folders: tuple[tuple[UUID, str], ...],
) -> tuple[str, InlineKeyboardMarkup]:
    """Connected bot home: one channel, then only active folders with live goods.

    The caller obtains the folders through project/bot-scoped list_folders;
    owner-authored folder names are never renamed or translated.
    """
    rows = [[InlineKeyboardButton(
        text=channel_name, callback_data="buyer:channel", style="success",
    )]]
    rows.extend([
        InlineKeyboardButton(
            text=name, callback_data="fs:folder:" + encode_uuid(folder_id),
            style="success",
        ),
    ] for folder_id, name in folders)
    rows.append([InlineKeyboardButton(
        text="🌐 Language / Язык", callback_data="lng:menu", style="success",
    )])
    return tr(
        "🔻 Выберите канал или 📁 папку с файлами",
        "🔻 Choose the channel or 📁 a file folder",
    ), InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_screen(
    offers: tuple[BuyerOffer, ...], *, currency: str | None = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    currencies = available_currencies(offers)
    if not currencies:
        return (
            tr(
                "💎 <b>Подписки</b>\n\n"
                "✨ Пока нет тарифов, доступных для покупки. Загляните позже! 👋",
                "💎 <b>Subscriptions</b>\n\n"
                "✨ No plans are available for purchase yet. Please check back later! 👋",
            ),
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🌐 Language / Язык",
                    callback_data="lng:menu", style="success",
                )],
            ]),
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
        tr("💎 <b>Подписки</b>", "💎 <b>Subscriptions</b>"),
        tr(
            "👋 Добро пожаловать! Выберите валюту и подходящий тариф — "
            "с оплатой поможем на следующем шаге.",
            "👋 Welcome! Choose a currency and the plan that suits you. "
            "We’ll guide you through payment next.",
        ),
        "",
        tr("🌍 Валюта: <b>", "🌍 Currency: <b>")
        + _currency_icon(chosen) + " " + escape(chosen) + "</b>",
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
            "✨ <b>" + _plan_summary(
                option.plan_name, period,
                duration_only=is_period_only_plan_name(
                    option.plan_name, option.duration_value, option.duration_unit,
                ),
            ) + "</b>"
            + " — " + escape(price + " " + chosen)
        )
        button_plan = (
            period if is_period_only_plan_name(
                option.plan_name, option.duration_value, option.duration_unit,
            )
            else option.plan_name[:23] + " · " + period
        )
        rows.append([InlineKeyboardButton(
            text=(
                "💎 " + button_plan + " · " + price + " " + chosen
            )[:64],
            callback_data=(
                f"offer:{encode_uuid(option.plan_price_id)}:{current_page}"
            ),
        )])
    if total_pages > 1:
        nav = []
        if current_page > 0:
            nav.append(InlineKeyboardButton(
                text=tr("⬅️ Назад", "⬅️ Back"),
                callback_data=f"cat:{chosen}:{current_page - 1}",
            ))
        if current_page + 1 < total_pages:
            nav.append(InlineKeyboardButton(
                text=tr("Далее ➡️", "Next ➡️"),
                callback_data=f"cat:{chosen}:{current_page + 1}",
            ))
        rows.append(nav)
        lines.extend(["", tr(f"Страница {current_page + 1} из {total_pages}",
                              f"Page {current_page + 1} of {total_pages}")])
    lines.extend(["", tr(
        "👇 Нажмите на тариф ниже. Способ оплаты выберете на следующем шаге. 💳",
        "👇 Tap a plan below. You’ll choose a payment method next. 💳",
    )])
    rows.append([InlineKeyboardButton(
        text="🌐 Language / Язык",
        callback_data="lng:menu",
        style="success",
    )])
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
        tr(
            "💎 <b>Оформление подписки</b>\n"
            "✨ Отличный выбор! Осталось выбрать удобный способ оплаты.\n\n",
            "💎 <b>Subscription checkout</b>\n"
            "✨ Great choice! Just pick the payment method you prefer.\n\n",
        )
        + "📣 <b>" + escape(current.resource_label) + "</b>\n"
        + (
            ""
            if is_period_only_plan_name(
                option.plan_name, option.duration_value, option.duration_unit,
            )
            else "🎟️ <b>" + escape(option.plan_name) + "</b>\n"
        )
        + "🗓️ " + escape(period) + "\n"
        + "💰 <b>" + escape(price + " " + option.currency_code) + "</b>\n\n"
        + tr("👇 <b>Как вам удобнее оплатить?</b>",
             "👇 <b>How would you like to pay?</b>")
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
        text=tr("⬅️ Все тарифы", "⬅️ All plans"),
        callback_data=f"cat:{option.currency_code}:{page}",
    )])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def rich_catalog_html(
    offers: tuple[BuyerOffer, ...], *, currency: str | None = None,
    page: int = 0, notice: str | None = None,
) -> str | None:
    """A native Rich plan table using the exact eligible catalog subset.

    The legacy catalog_screen remains the source of truth for currency picker,
    pagination and purchase callback data. This is presentation only.
    """
    currencies = available_currencies(offers)
    if not currencies:
        return None
    chosen = currency if currency in currencies else currencies[0]
    selected = [offer for offer in offers if offer.option.currency_code == chosen]
    total_pages = max(1, (len(selected) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = max(0, min(page, total_pages - 1))
    visible = selected[current_page * PAGE_SIZE:(current_page + 1) * PAGE_SIZE]
    parts = []
    if notice:
        # Keep the post-cancellation notification in the same Rich screen.
        # Escape unexpected markup rather than treating it as Telegram HTML.
        parts.append("<blockquote><b>" + escape(notice) + "</b></blockquote>")
    parts.extend([
        "<h3>" + tr("💎 Подписки", "💎 Subscriptions") + "</h3>",
        "<p>" + tr("🌍 Валюта: ", "🌍 Currency: ")
        + _currency_icon(chosen) + " <b>" + escape(chosen) + "</b></p>",
    ])
    active_resource: str | None = None
    for offer in visible:
        if offer.resource_label != active_resource:
            if active_resource is not None:
                parts.append("</table>")
            active_resource = offer.resource_label
            parts.extend([
                "<h4>📢 " + escape(active_resource) + "</h4>",
                "<table bordered compact>",
                "<tr><th>" + tr("🎟 Тариф", "🎟 Plan") + "</th><th>"
                + tr("💰 Цена", "💰 Price") + "</th></tr>",
            ])
        option = offer.option
        duration = format_period(option.duration_value, option.duration_unit)
        label = (
            duration if is_period_only_plan_name(
                option.plan_name, option.duration_value, option.duration_unit,
            )
            else option.plan_name + " · " + duration
        )
        amount = format_amount(option.amount, option.currency_precision)
        parts.append(
            "<tr><td>" + escape(label) + "</td><td>"
            + escape(amount + " " + chosen) + "</td></tr>"
        )
    if active_resource is not None:
        parts.append("</table>")
    if total_pages > 1:
        parts.append("<p>" + tr(
            f"Страница {current_page + 1} из {total_pages}",
            f"Page {current_page + 1} of {total_pages}",
        ) + "</p>")
    parts.append("<p>👇 " + tr(
        "Выберите тариф ниже. Способ оплаты будет следующим шагом.",
        "Choose a plan below. Payment methods come next.",
    ) + "</p>")
    return "\n".join(parts)
