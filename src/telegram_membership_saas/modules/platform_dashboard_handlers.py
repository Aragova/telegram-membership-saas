"""Private ViMi Pay operator menu. No tenant settings or payment mutation.

One Telegram account can navigate between platform and personal bot registry,
but each button performs a fresh server-side role check.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.localization import current_language, tr
from telegram_membership_saas.modules.merchant_finances import platform_merchant_finances
from telegram_membership_saas.modules.platform_risk import platform_merchant_risk_report
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_dashboard import (
    PAGE_SIZE,
    PERIOD_LABELS,
    PERIODS,
    fmt_turnover,
    is_configured_platform_owner,
    platform_report,
)
from telegram_membership_saas.modules.platform_usage import (
    BOT_EVENT_LABELS,
    EVENT_LABELS,
    platform_usage_report,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
from telegram_membership_saas.telegram_menus import set_chat_commands

PERIOD_LABELS_EN = {
    "day": "Today",
    "7": "7 days",
    "30": "30 days",
    "365": "Year",
    "all": "All time",
}

EVENT_LABELS_EN = {
    "bot_connected": "Bot connections",
    "bot_removed": "Bot removals",
    "bot_disabled": "Bot disables",
    "bot_reenabled": "Bot reactivations",
    "bot_deleting": "Deletion requests",
    "plan_published": "Plan publications",
    "payment_approved": "Approved payments",
    "customer_joined": "New customers",
    "subscription_started": "New subscriptions",
    "subscription_renewed": "Subscription renewals",
}

BOT_EVENT_LABELS_EN = {
    "bot_connected": "connected",
    "bot_removed": "removed",
    "bot_disabled": "disabled",
    "bot_reenabled": "reactivated",
    "bot_deleting": "removal requested",
}


def _period_label(value: str) -> str:
    return tr(PERIOD_LABELS[value], PERIOD_LABELS_EN[value])


def _status_label(value: str) -> str:
    states = {
        "registered": ("зарегистрирован", "registered"),
        "running": ("работает", "running"),
        "stale": ("нет сигнала", "stale"),
        "stopped": ("остановлен", "stopped"),
        "disabled": ("отключён", "disabled"),
        "deleting": ("удаляется", "deleting"),
        "unknown": ("неизвестно", "unknown"),
        "active": ("активен", "active"),
        "starting": ("запускается", "starting"),
        "failed": ("ошибка", "failed"),
    }
    labels = states.get(value)
    return tr(*labels) if labels is not None else value


class PlatformOwnerStartFilter(BaseFilter):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user is not None
            and is_configured_platform_owner(
                message.from_user.id, self.settings,
            )
        )


def _button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="plat:" + data)


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _period_rows(current: str, *, section: str) -> list[list[InlineKeyboardButton]]:
    result = [
        _button(
            ("✓ " if period == current else "") + _period_label(period),
            f"{section}:{period}:0",
        ) for period in PERIODS
    ]
    return [result[:3], result[3:]]


def _summary_text(snapshot) -> str:
    stats = snapshot.counts
    text = [
        tr("✦ <b>ViMi Pay · Платформа</b>", "✦ <b>ViMi Pay · Platform</b>"),
        tr("Статистика всей сети", "Platform-wide statistics"),
        "",
        tr(f"🤖 Подключённых ботов: <b>{stats['bots']}</b>",
           f"🤖 Connected bots: <b>{stats['bots']}</b>"),
        tr(f"✅ Зарегистрировано: {stats['bots_active']}",
           f"✅ Registered: {stats['bots_active']}"),
        tr(f"🟢 Работают сейчас: {stats['bots_running']}",
           f"🟢 Running now: {stats['bots_running']}"),
        tr(f"▫️ Отключено: {stats['bots_disabled']}",
           f"▫️ Disabled: {stats['bots_disabled']}"),
        tr(f"👥 Владельцев: <b>{stats['owners']}</b>",
           f"👥 Owners: <b>{stats['owners']}</b>"),
        tr(f"📢 Каналов и групп: {stats['resources']}",
           f"📢 Channels and groups: {stats['resources']}"),
        tr(f"💎 Опубликованных тарифов: {stats['plans_published']}",
           f"💎 Published plans: {stats['plans_published']}"),
        tr(f"📝 Черновиков тарифов: {stats['plans_draft']}",
           f"📝 Draft plans: {stats['plans_draft']}"),
        tr(f"🔐 Активных подписок: {stats['subscriptions_active']}",
           f"🔐 Active subscriptions: {stats['subscriptions_active']}"),
        tr(f"📋 Заявок на проверке: {stats['requests_pending']}",
           f"📋 Pending reviews: {stats['requests_pending']}"),
        "",
        f"<b>{_period_label(snapshot.period)}</b>",
        tr(f"Новых ботов: {stats['bots_new_period']}",
           f"New bots: {stats['bots_new_period']}"),
        tr(f"Новых подписок: {stats['subscriptions_new_period']}",
           f"New subscriptions: {stats['subscriptions_new_period']}"),
        tr(f"Подтверждённых оплат: {stats['payments_period']}",
           f"Approved payments: {stats['payments_period']}"),
        "",
        tr("💰 <b>Оборот клиентов по валютам</b>",
           "💰 <b>Client turnover by currency</b>"),
    ]
    if snapshot.turnover:
        text.extend(
            "• " + escape(str(item["currency"])) + " — "
            + escape(fmt_turnover(str(item["amount"])))
            + tr(" (операций: ", " (transactions: ") + str(item["count"]) + ")"
            for item in snapshot.turnover
        )
    else:
        text.append(tr("Подтверждённых платежей за период нет.",
                       "No approved payments in this period."))
    text.extend([
        "",
        tr("<i>Суммы подтверждены администраторами каналов, "
           "но не проверены ViMi Pay в банках.</i>",
           "<i>Amounts were approved by channel admins, "
           "but ViMi Pay has not verified bank transfers.</i>"),
        tr("Выручка ViMi Pay: <b>биллинг ещё не запущен</b>.",
           "ViMi Pay revenue: <b>billing is not yet enabled</b>."),
    ])
    return "\n".join(text)

def _bot_list(snapshot) -> str:
    lines = [
        tr("✦ <b>Подключённые боты</b>", "✦ <b>Connected bots</b>"),
        tr("Период: ", "Period: ") + _period_label(snapshot.period),
        "",
    ]
    if not snapshot.bots:
        lines.append(tr("На этой странице нет подключений.",
                        "No connections on this page."))
    for idx, bot in enumerate(snapshot.bots, 1):
        lines.extend([
            f"<b>{idx + snapshot.page * PAGE_SIZE}. "
            + escape("@" + bot["username"]) + "</b>",
            tr("Работа: ", "Runtime: ")
            + escape(_status_label(bot["runtime_state"])),
            tr("Тарифов: ", "Plans: ") + str(bot["plans"])
            + tr(" · Подписок: ", " · Subscriptions: ") + str(bot["subscriptions"])
            + tr(" · Оплат: ", " · Payments: ") + str(bot["payments_period"]),
            "",
        ])
    lines.append(
        tr("Страница ", "Page ") + str(snapshot.page + 1)
        + tr(" из ", " of ") + str(max(1, snapshot.pages))
    )
    return "\n".join(lines)

def _bot_details(bot: dict, *, period: str) -> str:
    currency_rows = bot.get("gmv_by_currency") or []
    finance = (
        "\n".join(
            "• " + escape(item["currency"]) + " — "
            + escape(fmt_turnover(str(item["amount"])))
            for item in currency_rows
        ) if currency_rows else tr(
            "Подтверждённых оплат за период нет.",
            "No approved payments in this period.",
        )
    )
    return (
        tr("✦ <b>Статистика бота</b>\n\n",
           "✦ <b>Bot statistics</b>\n\n")
        + "🤖 <b>@" + escape(bot["username"]) + "</b>\n"
        + tr("Проект: ", "Project: ") + escape(bot["project_name"]) + "\n"
        + tr("Подключение: ", "Connection: ")
        + escape(_status_label(bot["status"])) + "\n"
        + tr("Работа: ", "Runtime: ")
        + escape(_status_label(bot["runtime_state"])) + "\n\n"
        + tr("📢 Ресурсов: ", "📢 Resources: ") + str(bot["resources"]) + "\n"
        + tr("💎 Опубликованных тарифов: ", "💎 Published plans: ")
        + str(bot["plans"]) + "\n"
        + tr("🔐 Активных подписок: ", "🔐 Active subscriptions: ")
        + str(bot["subscriptions"]) + "\n"
        + tr("💳 Подтверждённых оплат (", "💳 Approved payments (")
        + escape(_period_label(period)) + "): "
        + str(bot["payments_period"]) + "\n\n"
        + tr("💰 <b>Оборот по валютам</b>\n",
             "💰 <b>Turnover by currency</b>\n")
        + finance + "\n\n"
        + tr("<i>Только агрегаты. Управление каналом и платежами "
             "осуществляется в собственном боте владельца.</i>",
             "<i>Aggregate figures only. Channels and payments "
             "are managed in the owner's connected bot.</i>")
    )

def _usage_text(snapshot, *, timezone: str) -> str:
    lines = [
        tr("✦ <b>ViMi Pay · Использование</b>",
           "✦ <b>ViMi Pay · Usage</b>"),
        tr("Журнал работы платформы", "Platform activity history"),
        "",
        "<b>" + escape(_period_label(snapshot.period)) + "</b>",
        tr("Событий: ", "Events: ") + str(snapshot.event_total),
    ]
    for kind, label in EVENT_LABELS.items():
        count = snapshot.counts.get(kind, 0)
        lines.append("• " + tr(label, EVENT_LABELS_EN[kind]) + ": " + str(count))
    lines.extend(["", tr("💳 <b>Платежи клиентов по валютам</b>",
                         "💳 <b>Client payments by currency</b>")])
    if snapshot.turnover:
        for item in snapshot.turnover:
            lines.append(
                "• " + escape(item["currency"]) + " — "
                + escape(fmt_turnover(str(item["amount"])))
                + tr(" (подтверждений: ", " (approvals: ")
                + str(item["count"]) + ")"
            )
    else:
        lines.append(tr("Нет подтверждённых платежей за период.",
                        "No approved payments in this period."))
    lines.extend(["", tr("🤖 <b>История подключений и удалений</b>",
                         "🤖 <b>Connection and removal history</b>")])
    for item in snapshot.history:
        occurred = datetime.fromisoformat(item["occurred_at"])
        timestamp = occurred.astimezone(ZoneInfo(timezone)).strftime("%d.%m %H:%M")
        lines.append(
            "• " + timestamp + " — @"
            + escape(item["bot_username"] or "unknown") + ": "
            + escape(tr(
                BOT_EVENT_LABELS.get(item["event_type"], "событие"),
                BOT_EVENT_LABELS_EN.get(item["event_type"], "event"),
            ))
        )
    if not snapshot.history:
        lines.append(tr("На этой странице нет событий.",
                        "No events on this page."))
    if snapshot.pages > 1:
        lines.append(
            tr("\nСтраница ", "\nPage ") + str(snapshot.page + 1)
            + tr(" из ", " of ") + str(snapshot.pages)
        )
    lines.extend([
        "",
        tr("<i>Журнал сохраняет удалённые подключения. Метрики "
           "не являются выставленными счетами ViMi Pay. "
           "Платежи подтверждены администраторами каналов.</i>",
           "<i>Deleted connections remain in this ledger. These are "
           "not ViMi Pay invoices. Channel admins confirmed the payments.</i>"),
    ])
    return "\n".join(lines)

def _merchant_text(snapshot) -> str:
    lines = [
        tr("✦ <b>ViMi Pay · Финансы бизнесов</b>",
           "✦ <b>ViMi Pay · Merchant finances</b>"),
        "<b>" + escape(_period_label(snapshot.period)) + "</b>",
        tr("Бизнесов с подтверждёнными платежами: ",
           "Businesses with approved payments: ") + str(snapshot.merchants_total),
    ]
    if not snapshot.businesses:
        lines.append(tr("Нет подтверждённых оплат за период.",
                        "No approved payments in this period."))
    for business in snapshot.businesses:
        # Existing project names can contain embedded line separators.
        # Collapse whitespace before HTML escaping so the chunker never
        # splits a <b>label</b> element across Telegram messages.
        label = " ".join(str(business["label"]).split())
        if label == "Archived connection":
            label = tr("Архивное подключение", "Archived connection")
        lines.extend([
            "",
            "💼 <b>" + escape(label[:90]) + "</b>",
            tr("Подтверждённых операций: ", "Approved transactions: ")
            + str(business["payments"]),
        ])
        for currency in business["currencies"]:
            lines.append(
                "• " + escape(str(currency["currency"])) + " — "
                + escape(fmt_turnover(str(currency["amount"])))
                + tr(" (операций: ", " (transactions: ")
                + str(currency["count"]) + ")"
            )
    if snapshot.unmapped_payments:
        lines.extend([
            "",
            tr("⚠️ Операций без установленного исторического бизнеса: ",
               "⚠️ Approvals with no attributable historical business: ")
            + str(snapshot.unmapped_payments),
        ])
    if snapshot.pages > 1:
        lines.append(
            tr("\nСтраница ", "\nPage ") + str(snapshot.page + 1)
            + tr(" из ", " of ") + str(snapshot.pages)
        )
    lines.extend([
        "",
        tr("<i>Только подтверждения администраторов каналов, "
           "без независимой банковской проверки. "
           "Это не выручка ViMi Pay и не счёт к оплате.</i>",
           "<i>Channel-admin approvals, not independent bank verification. "
           "This is neither ViMi Pay revenue nor an invoice.</i>"),
    ])
    return "\n".join(lines)


def _merchant_text_chunks(snapshot, *, limit: int = 3500) -> tuple[str, ...]:
    """Split a financial report only between complete HTML-safe lines.

    Telegram rejects a message longer than 4096 characters. Splitting on
    rendered lines (never inside escaped amounts or tags) preserves every
    currency, the summary and the final navigation keyboard.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in _merchant_text(snapshot).split("\n"):
        if len(line) > limit:
            raise ValueError("Merchant finance report line exceeds safe Telegram size")
        required = len(line) + (1 if current else 0)
        if current and size + required > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
            required = len(line)
        current.append(line)
        size += required
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks)


def _risk_review_text(snapshot, *, timezone: str) -> str:
    """Review signals are not fraud verdicts or automatic actions."""
    lines = [
        tr("✦ <b>ViMi Pay · Проверка каналов</b>",
           "✦ <b>ViMi Pay · Channel review</b>"),
        "<b>" + escape(_period_label(snapshot.period)) + "</b>",
        tr("Сигналов для проверки: ", "Review signals: ") + str(snapshot.total),
    ]
    if not snapshot.observations:
        lines.append(tr("Совпадений между разными владельцами нет.",
                        "No cross-owner channel observations."))
    for item in snapshot.observations:
        channel = " ".join(str(item["channel_name"]).split())
        if channel == "Channel unavailable":
            channel = tr("Название недоступно", "Name unavailable")
        project = " ".join(str(item["project_name"]).split())
        if project == "Archived connection":
            project = tr("Архивное подключение", "Archived connection")
        timestamp = datetime.fromisoformat(str(item["observed_at"]))
        local_date = timestamp.astimezone(
            ZoneInfo(timezone)
        ).strftime("%d.%m.%Y %H:%M")
        lines.extend([
            "",
            "⚠️ <b>" + escape(channel[:120]) + "</b>",
            tr("Подключение: ", "Connection: ") + escape(project[:120]),
            tr("ID канала: ", "Channel ID: ") + "<code>"
            + escape(str(item["chat_id"])) + "</code>",
            tr("Зафиксировано: ", "Observed: ") + escape(local_date),
        ])
    if snapshot.pages > 1:
        lines.append(
            tr("\nСтраница ", "\nPage ") + str(snapshot.page + 1)
            + tr(" из ", " of ") + str(snapshot.pages)
        )
    lines.extend([
        "",
        tr("<i>Совпадение канала у разных владельцев — только повод "
           "проверить подключение. Законная передача возможна. "
           "ViMi Pay ничего не блокирует автоматически.</i>",
           "<i>A channel observed under different owners is a review "
           "signal, not proof of fraud. Legitimate transfers are possible. "
           "No automatic blocking occurs.</i>"),
    ])
    return "\n".join(lines)


def create_platform_dashboard_router(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings | None = None,
) -> Router:
    config = settings or get_settings()
    router = Router(name="platform-dashboard")

    async def read_report(user, *, period: str, page: int = 0):
        if not is_configured_platform_owner(user.id, config):
            raise PermissionError("Platform access denied")
        actor_id = await ensure_telegram_actor(
            factory, telegram_id=user.id, username=user.username,
            first_name=user.first_name,
        )
        return await platform_report(
            factory, actor_user_id=actor_id, actor_telegram_id=user.id,
            period=period, page=page, settings=config,
        )

    async def send_platform_home(bot: Bot, user, *, period: str) -> None:
        report = await read_report(user, period=period)
        rows = _period_rows(period, section="home")
        rows.extend([
            [_button(tr("🤖 Все подключённые боты", "🤖 Connected bots"), f"bots:{period}:0")],
            [_button(tr("📊 Использование и история", "📊 Usage & history"), f"usage:{period}:0")],
            [_button(tr("💼 Финансы бизнесов", "💼 Merchant finances"), f"merchants:{period}:0")],
            [_button(tr("🛡 Проверка каналов", "🛡 Channel review"), f"risks:{period}:0")],
            [InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")],
            [InlineKeyboardButton(
                text=tr("🤖 Мои боты · личный режим", "🤖 My bots · personal mode"),
                callback_data="own:projects",
            )],
        ])
        await bot.send_message(
            user.id, _summary_text(report),
            parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    @router.message(
        Command("start", "platform"), PlatformOwnerStartFilter(config),
    )
    async def platform_start(message: Message, bot: Bot) -> None:
        try:
            await set_chat_commands(
                bot, chat_id=message.from_user.id, role="platform",
                language=current_language(),
            )
            await send_platform_home(bot, message.from_user, period="30")
        except Exception:
            # No internals, credentials, or SQL errors in operator chat.
            await bot.send_message(
                message.from_user.id,
                tr("👑 Панель платформы пока недоступна. "
                   "Проверьте подключение и права оператора.",
                   "👑 Platform dashboard is temporarily unavailable. "
                   "Check operator access and connectivity."),
            )

    @router.callback_query(F.data.startswith("plat:"))
    async def platform_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
            or not is_configured_platform_owner(query.from_user.id, config)
        ):
            await query.answer(
                tr("Нет доступа к панели платформы", "Platform access denied"),
                show_alert=True,
            )
            return
        tokens = (query.data or "").split(":")
        try:
            if tokens[1:] == ["home"]:
                await send_platform_home(bot, query.from_user, period="30")
            elif len(tokens) == 4 and tokens[1] == "home":
                _, _, period, page_token = tokens
                if period not in PERIODS or page_token != "0":
                    raise ValueError("Некорректный период")
                await send_platform_home(
                    bot, query.from_user, period=period,
                )
            elif len(tokens) == 4 and tokens[1] == "usage":
                _, _, period, page_token = tokens
                if period not in PERIODS or (
                    not page_token.isascii()
                    or not page_token.isdecimal()
                ):
                    raise ValueError("Некорректный период использования")
                page = int(page_token)
                if page > 100000:
                    raise ValueError("Страница недоступна")
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_usage_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id,
                    period=period, page=page, settings=config,
                )
                rows = _period_rows(period, section="usage")
                nav = []
                if page > 0:
                    nav.append(_button(
                        tr("⬅️ Предыдущие", "⬅️ Previous"), f"usage:{period}:{page - 1}",
                    ))
                if page + 1 < report.pages:
                    nav.append(_button(
                        tr("Следующие ➡️", "Next ➡️"), f"usage:{period}:{page + 1}",
                    ))
                if nav:
                    rows.append(nav)
                rows.append([_button(tr("⬅️ Платформа", "⬅️ Platform"), f"home:{period}:0")])
                await bot.send_message(
                    query.from_user.id,
                    _usage_text(report, timezone=config.platform_timezone),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif len(tokens) == 4 and tokens[1] == "merchants":
                _, _, period, page_token = tokens
                if period not in PERIODS or (
                    not page_token.isascii() or not page_token.isdecimal()
                ):
                    raise ValueError("Некорректная страница финансов")
                page = int(page_token)
                if page > 100000:
                    raise ValueError("Страница недоступна")
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_merchant_finances(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id,
                    period=period, page=page, settings=config,
                )
                rows = _period_rows(period, section="merchants")
                nav = []
                if page > 0:
                    nav.append(_button(
                        tr("⬅️ Предыдущие", "⬅️ Previous"),
                        f"merchants:{period}:{page - 1}",
                    ))
                if page + 1 < report.pages:
                    nav.append(_button(
                        tr("Следующие ➡️", "Next ➡️"),
                        f"merchants:{period}:{page + 1}",
                    ))
                if nav:
                    rows.append(nav)
                rows.append([_button(
                    tr("⬅️ Платформа", "⬅️ Platform"), f"home:{period}:0",
                )])
                chunks = _merchant_text_chunks(report)
                for index, chunk in enumerate(chunks):
                    await bot.send_message(
                        query.from_user.id, chunk, parse_mode="HTML",
                        reply_markup=_keyboard(rows) if index == len(chunks) - 1 else None,
                    )
            elif len(tokens) == 4 and tokens[1] == "risks":
                _, _, period, page_token = tokens
                if period not in PERIODS or (
                    not page_token.isascii() or not page_token.isdecimal()
                ):
                    raise ValueError("Некорректная страница проверок")
                page = int(page_token)
                if page > 100000:
                    raise ValueError("Страница недоступна")
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_merchant_risk_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id,
                    period=period, page=page, settings=config,
                )
                rows = _period_rows(period, section="risks")
                nav = []
                if page > 0:
                    nav.append(_button(
                        tr("⬅️ Предыдущие", "⬅️ Previous"),
                        f"risks:{period}:{page - 1}",
                    ))
                if page + 1 < report.pages:
                    nav.append(_button(
                        tr("Следующие ➡️", "Next ➡️"),
                        f"risks:{period}:{page + 1}",
                    ))
                if nav:
                    rows.append(nav)
                rows.append([_button(
                    tr("⬅️ Платформа", "⬅️ Platform"), f"home:{period}:0",
                )])
                await bot.send_message(
                    query.from_user.id,
                    _risk_review_text(report, timezone=config.platform_timezone),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif len(tokens) == 4 and tokens[1] == "bots":
                _, _, period, page_token = tokens
                if period not in PERIODS or not page_token.isascii() or (
                    not page_token.isdecimal()
                ):
                    raise ValueError("Некорректная страница")
                page = int(page_token)
                report = await read_report(query.from_user, period=period, page=page)
                rows = []
                for idx, item in enumerate(report.bots):
                    rows.append([_button(
                        "🤖 @" + item["username"][:43],
                        f"detail:{period}:{page}:{idx}:"
                        + encode_uuid(UUID(item["bot_instance_id"])),
                    )])
                nav = []
                if page > 0:
                    nav.append(_button("⬅️", f"bots:{period}:{page - 1}"))
                if page + 1 < report.pages:
                    nav.append(_button("➡️", f"bots:{period}:{page + 1}"))
                if nav:
                    rows.append(nav)
                rows.append([_button(tr("⬅️ Платформа", "⬅️ Platform"), f"home:{period}:0")])
                await bot.send_message(
                    query.from_user.id, _bot_list(report),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif len(tokens) == 6 and tokens[1] == "detail":
                _, _, period, page_token, index_token, bot_token = tokens
                if period not in PERIODS or not page_token.isascii() or (
                    not page_token.isdecimal()
                ) or not index_token.isascii() or (
                    not index_token.isdecimal()
                ):
                    raise ValueError("Некорректная карточка")
                page = int(page_token)
                index = int(index_token)
                if index >= PAGE_SIZE:
                    raise ValueError("Карточка недоступна")
                report = await read_report(query.from_user, period=period, page=page)
                if index >= len(report.bots) or bot_token != encode_uuid(
                    UUID(report.bots[index]["bot_instance_id"])
                ):
                    raise ValueError("Карточка устарела. Обновите список.")
                await bot.send_message(
                    query.from_user.id,
                    _bot_details(report.bots[index], period=period),
                    parse_mode="HTML", reply_markup=_keyboard([
                        [_button(tr("⬅️ Все боты", "⬅️ All bots"), f"bots:{period}:{page}")],
                    ]),
                )
            else:
                raise ValueError("Кнопка устарела")
        except (PermissionError, ValueError):
            await query.answer(
                tr("Недоступно. Откройте /platform заново.",
                   "Unavailable. Reopen /platform."),
                show_alert=True,
            )
            return
        except Exception:
            await query.answer(
                tr("Отчёт временно недоступен.",
                   "Report temporarily unavailable."),
                show_alert=True,
            )
            return
        await query.answer()

    return router
