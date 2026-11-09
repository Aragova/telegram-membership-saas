"""Buyer read-only account: actual subscriptions and payment requests.

All lookups are scoped by the connected bot project AND the Telegram user;
never create a ProjectCustomer just to show an empty account.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape
from uuid import UUID


from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.payments import PaymentRequest
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_commerce.service import (
    encode_uuid,
    format_amount,
)
from telegram_membership_saas.support_links import support_button
from telegram_membership_saas.telegram_dates import native_telegram_time

ACCOUNT_PAGE_SIZE = 5
ACCOUNT_MAX_PAGE = 1000


@dataclass(frozen=True, slots=True)
class BuyerSubscription:
    name: str
    status: str
    ends_at: datetime | None


@dataclass(frozen=True, slots=True)
class BuyerRequest:
    id: UUID
    name: str
    status: str
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class BuyerAccount:
    subscriptions_count: int = 0
    requests_count: int = 0
    subscriptions: tuple[BuyerSubscription, ...] = ()
    requests: tuple[BuyerRequest, ...] = ()
    timezone: str = "Europe/Moscow"


async def load_buyer_account(
    session: AsyncSession, *, project_id: UUID,
    telegram_user_id: int, section: str = "home", page: int = 0,
) -> BuyerAccount:
    if section not in ("home", "subs", "reqs") or not 0 <= page <= ACCOUNT_MAX_PAGE:
        raise ValueError("Invalid account section or page")
    customer_id = await session.scalar(
        select(ProjectCustomer.id)
        .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
        .where(
            ProjectCustomer.project_id == project_id,
            TelegramUser.telegram_user_id == telegram_user_id,
        )
    )
    if customer_id is None:
        return BuyerAccount()

    subs_filter = (
        Subscription.project_id == project_id,
        Subscription.customer_id == customer_id,
    )
    reqs_filter = (
        PaymentRequest.project_id == project_id,
        PaymentRequest.customer_id == customer_id,
    )
    subs_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(*subs_filter),
    )
    reqs_count = await session.scalar(
        select(func.count()).select_from(PaymentRequest).where(*reqs_filter),
    )
    timezone = await session.scalar(
        select(Project.timezone).where(Project.id == project_id),
    )
    subscriptions: tuple[BuyerSubscription, ...] = ()
    requests: tuple[BuyerRequest, ...] = ()
    if section == "subs":
        rows = (await session.execute(
            select(Subscription, Plan.name)
            .join(Plan, and_(
                Plan.id == Subscription.plan_id,
                Plan.project_id == Subscription.project_id,
            ))
            .where(*subs_filter)
            .order_by(Subscription.updated_at.desc(), Subscription.id)
            .offset(page * ACCOUNT_PAGE_SIZE)
            .limit(ACCOUNT_PAGE_SIZE)
        )).all()
        subscriptions = tuple(
            BuyerSubscription(name=name, status=item.status,
                              ends_at=item.current_period_end)
            for item, name in rows
        )
    elif section == "reqs":
        rows = (await session.execute(
            select(PaymentRequest, Plan.name)
            .join(Plan, and_(
                Plan.id == PaymentRequest.plan_id,
                Plan.project_id == PaymentRequest.project_id,
            ))
            .where(*reqs_filter)
            .order_by(PaymentRequest.created_at.desc(), PaymentRequest.id)
            .offset(page * ACCOUNT_PAGE_SIZE)
            .limit(ACCOUNT_PAGE_SIZE)
        )).all()
        requests = tuple(
            BuyerRequest(
                id=item.id, name=name, status=item.status,
                amount=item.final_amount, currency=item.currency_code,
            ) for item, name in rows
        )
    return BuyerAccount(
        subscriptions_count=int(subs_count or 0),
        requests_count=int(reqs_count or 0),
        subscriptions=subscriptions,
        requests=requests,
        timezone=timezone or "Europe/Moscow",
    )


def _buttons(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _button(ru: str, en: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=tr(ru, en), callback_data=callback)


def account_home_screen(account: BuyerAccount) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        tr("✦ <b>Мои покупки</b>\n\n", "✦ <b>My purchases</b>\n\n")
        + tr("Подписок: ", "Subscriptions: ") + str(account.subscriptions_count)
        + "\n" + tr("Платёжных заявок: ", "Payment requests: ")
        + str(account.requests_count) + "\n\n"
        + tr("Здесь сохраняются заявки и подписки, даже если вы "
             "вышли из оформления покупки. Просмотр ничего не отменяет.",
             "Your payment requests and subscriptions remain here "
             "even if you leave checkout. Viewing them never cancels anything.")
    )
    return text, _buttons(
        [_button("🔐 Подписки", "🔐 Subscriptions", "account:subs:0")],
        [_button("🧾 Заявки", "🧾 Payment requests", "account:reqs:0")],
        [_button("💎 Выбрать тариф", "💎 Browse plans", "account:catalog")],
        [_button("🛟 Поддержка", "🛟 Support", "account:support")],
    )


_SUB_STATUS = {
    "active": ("🟢 Активна", "🟢 Active"),
    "expired": ("⚪ Истекла", "⚪ Expired"),
    "cancelled": ("🔴 Завершена", "🔴 Ended"),
}
_REQUEST_STATUS = {
    "waiting_confirmation": ("🕒 Ожидает чека", "🕒 Awaiting receipt"),
    "waiting_review": ("🔎 На проверке", "🔎 Under review"),
    "approved": ("✅ Подтверждена", "✅ Approved"),
    "rejected": ("❌ Отклонена", "❌ Rejected"),
    "cancelled": ("⚪ Отменена", "⚪ Canceled"),
    "expired": ("⌛ Истекла", "⌛ Expired"),
}


def account_list_screen(
    account: BuyerAccount, *, section: str, page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    if section not in ("subs", "reqs") or not 0 <= page <= ACCOUNT_MAX_PAGE:
        raise ValueError("Invalid account section or page")
    count = account.subscriptions_count if section == "subs" else account.requests_count
    title = (
        tr("🔐 <b>Мои подписки</b>", "🔐 <b>My subscriptions</b>")
        if section == "subs"
        else tr("🧾 <b>Мои заявки</b>", "🧾 <b>Payment requests</b>")
    )
    lines = [title, ""]
    rows = []
    if section == "subs":
        for item in account.subscriptions:
            status = _SUB_STATUS.get(item.status)
            label = tr(*status) if status else tr("Неизвестно", "Unknown")
            if item.ends_at is None:
                end = tr("бессрочно", "lifetime")
            else:
                end = native_telegram_time(
                    item.ends_at, account.timezone, fmt="dt",
                )
            lines.append(
                "💎 <b>" + escape(item.name) + "</b>\n"
                + label + " · " + tr("До: ", "Until: ") + end + "\n"
            )
    else:
        for item in account.requests:
            status = _REQUEST_STATUS.get(item.status)
            label = tr(*status) if status else tr("Неизвестно", "Unknown")
            reference = "#" + item.id.hex[:10]
            lines.append(
                "💎 <b>" + escape(item.name) + "</b>\n"
                + label + " · " + escape(format_amount(item.amount, 18))
                + " " + escape(item.currency)
                + " · " + reference + "\n"
            )
            if item.status == "waiting_confirmation":
                button_name = item.name[:22]
                caption = (
                    tr("📎 Чек", "📎 Receipt")
                    + " · " + reference + " · " + button_name
                )
                rows.append([InlineKeyboardButton(
                    text=caption[:64],
                    callback_data="paid:" + encode_uuid(item.id),
                )])
    if not (account.subscriptions if section == "subs" else account.requests):
        lines.append(tr(
            "Здесь пока нет записей.", "Nothing to show here yet.",
        ))
    nav = []
    if page > 0:
        nav.append(_button("⬅️ Назад", "⬅️ Previous",
                           f"account:{section}:{page - 1}"))
    if (page + 1) * ACCOUNT_PAGE_SIZE < count:
        nav.append(_button("Далее ➡️", "Next ➡️",
                           f"account:{section}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend([
        [_button("⬅️ Мои покупки", "⬅️ My purchases", "account:home")],
        [_button("💎 К тарифам", "💎 Browse plans", "account:catalog")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def support_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = tr(
        "🛟 <b>Помощь с подпиской</b>\n\n"
        "• Тарифы и способы оплаты можно посмотреть в каталоге.\n"
        "• После оплаты прикрепите чек к своей заявке в разделе «Мои покупки».\n"
        "• Проверку перевода и вопросы о возврате решает администрация канала.\n"
        "• Никому не отправляйте пароли, коды Telegram и токены ботов.\n\n"
        "Если доступ задерживается, напишите в поддержку ViMi Pay. "
        "Укажите номер заявки, но не присылайте пароли или коды.",
        "🛟 <b>Membership support</b>\n\n"
        "• Browse plans and payment methods in the catalog.\n"
        "• After paying, attach your receipt from My purchases.\n"
        "• The channel administration reviews bank transfers and refund requests.\n"
        "• Never share passwords, Telegram codes or bot tokens.\n\n"
        "If access is delayed, contact ViMi Pay support with your "
        "request reference. Never send passwords or login codes.",
    )
    return text, _buttons(
        [_button("🧾 Мои покупки", "🧾 My purchases", "account:home")],
        [_button("💎 К тарифам", "💎 Browse plans", "account:catalog")],
        [support_button()],
    )
