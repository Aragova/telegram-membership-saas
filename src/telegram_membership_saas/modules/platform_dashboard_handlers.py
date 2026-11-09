"""Private ViMi Pay operator menu. No tenant settings or payment mutation.

One Telegram account can navigate between platform and personal bot registry,
but each button performs a fresh server-side role check.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
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
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_dashboard import (
    PAGE_SIZE,
    PERIOD_LABELS,
    PERIODS,
    fmt_turnover,
    is_configured_platform_owner,
    platform_report,
)
from telegram_membership_saas.modules.platform_risk import platform_merchant_risk_report
from telegram_membership_saas.modules.platform_usage import (
    BOT_EVENT_LABELS,
    EVENT_LABELS,
    platform_usage_report,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
from telegram_membership_saas.telegram_dates import native_telegram_time
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
        "error": ("ошибка", "error"),
        "queued": ("ожидает запуска", "queued"),
        "setup_needed": ("нужна настройка", "setup needed"),
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



async def _render_platform_screen(
    bot: Bot, *, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup, message_id: int | None = None,
) -> None:
    """Edit interactive platform screens in place; send if editing is unavailable."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML", reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                return
            if not any(marker in reason for marker in (
                "message can't be edited",
                "message to edit not found",
                "there is no text in the message to edit",
            )):
                raise
        except TelegramNotFound as exc:
            if "message to edit not found" not in str(exc).lower():
                raise
    # Never send a second copy after an uncertain network/transport error.
    await bot.send_message(
        chat_id, text, parse_mode="HTML", reply_markup=reply_markup,
    )


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

_RUNTIME_ICONS = {
    "running": "🟢",
    "starting": "🟡",
    "queued": "🟡",
    "setup_needed": "🟡",
    "stale": "🟠",
    "stopped": "🔴",
    "failed": "🔴",
    "error": "🔴",
    "disabled": "⚪",
    "deleting": "⚪",
    "registered": "⚪",
    "unknown": "⚪",
}


# UI-only thresholds. Never use these to mute/produce incident notifications.
# The DB report remains the sole source of truth for runtime and heartbeat.
_STARTUP_GRACE = timedelta(minutes=5)
_ERROR_TEXT = {
    "invalid_credentials": (
        "Проблема с токеном: требуется проверить подключение бота",
        "Bot token problem: check the bot connection",
    ),
    "telegram_check_failed": (
        "Telegram не подтвердил подключение: проверьте API и webhook",
        "Telegram connection check failed: verify API and webhook",
    ),
    "missing_admin_rights": (
        "Боту не хватает прав администратора в канале",
        "Bot is missing channel administrator rights",
    ),
    "startup_failed": (
        "Запуск завершился ошибкой; Supervisor повторит попытку",
        "Startup failed; Supervisor will retry",
    ),
    "worker_stopped": (
        "Рабочий процесс завершился; ожидается повторный запуск",
        "Worker stopped; waiting for retry",
    ),
    "lock_lost": (
        "Смена рабочего процесса или потеря блокировки; ожидается перезапуск",
        "Worker handoff or lease lost; waiting for restart",
    ),
}


def _heartbeat_time(bot: dict) -> datetime | None:
    """Parse only the authorized platform report, never raw exception content."""
    raw = bot.get("last_heartbeat")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None and moment.utcoffset() is not None else None


def _monitoring_diagnosis(
    bot: dict, *, now: datetime | None = None,
) -> tuple[str, str]:
    """A current observation, NOT a persistent incident or automatic alert.

    During a deployment the bot may briefly be queued, starting or stopped.
    Last heartbeat alone is not enough to prove a repeated failure.
    """
    runtime = str(bot.get("runtime_state") or "unknown")
    status = str(bot.get("status") or "unknown")
    seen = _heartbeat_time(bot)
    current = now or datetime.now(UTC)
    elapsed = current - seen if seen is not None else None

    if status == "disabled":
        return (tr("⚪ Отключён владельцем", "⚪ Disabled by owner"),
                tr("Это не авария", "This is not an incident"))
    if status == "deleting":
        return (tr("⚪ Удаляется", "⚪ Being removed"),
                tr("Это не авария", "This is not an incident"))
    if runtime == "running" and elapsed is not None and elapsed <= timedelta(seconds=120):
        return (tr("🟢 Работает", "🟢 Running"),
                tr("Последний записанный статус: работает", "Latest recorded state: running"))
    if runtime in {"starting", "queued", "setup_needed"}:
        return (
            "🟡 " + _status_label(runtime),
            tr("Бот настраивается или запускается. Это не подтверждённая авария.",
               "Bot is being configured or started. This is not a confirmed outage."),
        )
    if runtime in {"stale", "running"}:
        if elapsed is not None and elapsed <= _STARTUP_GRACE:
            return (
                tr("🟠 Сигнал задерживается", "🟠 Heartbeat delayed"),
                tr("Возможен прогрев после обновления. Повторите проверку через несколько минут.",
                   "This may be startup after deployment. Recheck in a few minutes."),
            )
        return (
            tr("🟠 Нет свежего сигнала", "🟠 No recent heartbeat"),
            tr("Проверьте Supervisor и Telegram. Один сигнал не подтверждает аварию.",
               "Check Supervisor and Telegram. A single observation does not confirm an outage."),
        )
    if runtime in {"error", "failed"}:
        code = str(bot.get("error_code") or "")
        is_restarting = (
            code in {"worker_stopped", "lock_lost"}
            and elapsed is not None and elapsed <= _STARTUP_GRACE
        )
        return (
            tr("🟡 Перезапускается", "🟡 Restarting")
            if is_restarting else
            tr("🔴 Ошибка запуска", "🔴 Startup error"),
            tr(*_ERROR_TEXT[code]) if code in _ERROR_TEXT else
            tr("Supervisor зафиксировал ошибку. Проверьте повторный запуск.",
               "Supervisor recorded an error. Check the next startup attempt."),
        )
    if runtime == "stopped":
        if elapsed is not None and elapsed <= _STARTUP_GRACE:
            return (
                tr("🟡 Недавно остановлен", "🟡 Recently stopped"),
                tr("Возможно, бот перезапускается после обновления. Проверьте повторно.",
                   "Bot may be restarting after deployment. Recheck before treating as outage."),
            )
        return (
            tr("🔴 Остановлен", "🔴 Stopped"),
            tr("Свежего сигнала нет. Проверьте Supervisor; "
               "одного снимка состояния недостаточно для вывода о постоянной аварии.",
               "No fresh heartbeat. Check Supervisor; "
               "one snapshot does not prove a persistent outage."),
        )
    return (
        tr("⚪ Нет подтверждения работы", "⚪ Runtime not confirmed"),
        tr("Нет актуального подтверждения от Supervisor; не считать аварией без проверки.",
           "No confirmed Supervisor signal; do not classify as an outage without checking."),
    )


def _bot_list_button_label(bot: dict) -> str:
    """One tappable row: defer false-red states during Supervisor warm-up."""
    state = bot["runtime_state"]
    marker = _RUNTIME_ICONS.get(state, "⚪")
    heartbeat = _heartbeat_time(bot)
    if heartbeat is not None and datetime.now(UTC) - heartbeat <= _STARTUP_GRACE:
        if state == "stopped" or (
            state == "error"
            and bot.get("error_code") in {"worker_stopped", "lock_lost"}
        ):
            marker = "🟡"
    metrics = (
        f" · 💎{bot['plans']} 🔐{bot['subscriptions']}"
        f" 💳{bot['payments_period']}"
    )
    handle = "@" + bot["username"]
    # Keep useful metrics visible even for the longest Telegram usernames.
    room = max(1, 64 - len(marker) - 1 - len(metrics))
    if len(handle) > room:
        handle = handle[:max(1, room - 1)] + "…"
    return f"{marker} {handle}{metrics}"


def _bot_list(snapshot) -> str:
    """The ONE global monitoring surface. Existing bot cards remain here.

    A fresh worker heartbeat is the authoritative liveness signal. Pending
    setup, queued, starting, unknown and stale bots are NOT asserted broken:
    they are merely not confirmed running and must be checked individually.
    """
    stats = snapshot.counts
    registered = int(stats["bots_active"])
    running = int(stats["bots_running"])
    disabled = int(stats["bots_disabled"])
    no_running_signal = max(0, registered - running)
    lines = [
        tr("✦ <b>Мониторинг · ViMi Pay</b>",
           "✦ <b>Monitoring · ViMi Pay</b>"),
        tr("🤖 Подключено: ", "🤖 Connected: ") + str(stats["bots"]),
        tr("🟢 Работают: ", "🟢 Running: ") + str(running),
        tr("🟠 Нет подтверждения работы: ", "🟠 No confirmed runtime: ")
        + str(no_running_signal),
        tr("⚪ Отключено: ", "⚪ Disabled: ") + str(disabled),
        "",
        tr("Период финансов: ", "Finance period: ") + _period_label(snapshot.period),
        tr("На странице: ", "On this page: ") + str(len(snapshot.bots)),
        "",
    ]
    if snapshot.bots:
        lines.extend([
            tr("🟢 работают · 🟡 запускаются · 🟠 нет сигнала",
               "🟢 running · 🟡 starting · 🟠 stale"),
            tr("🔴 остановлены/ошибка · ⚪ другие статусы",
               "🔴 stopped/failed · ⚪ other states"),
            tr("💎 тарифы · 🔐 подписки · 💳 оплаты за период",
               "💎 plans · 🔐 subscriptions · 💳 period payments"),
            "",
            tr("👇 Выберите бота ниже", "👇 Select a bot below"),
        ])
    else:
        lines.append(tr("На этой странице нет подключений.",
                        "No connections on this page."))
    lines.extend([
        "",
        tr("Страница ", "Page ") + str(snapshot.page + 1)
        + tr(" из ", " of ") + str(max(1, snapshot.pages)),
    ])
    return "\n".join(lines)


def _bot_details(
    bot: dict, *, period: str, timezone: str = "Europe/Moscow",
) -> str:
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
    condition, explanation = _monitoring_diagnosis(bot)
    heartbeat = _heartbeat_time(bot)
    last_signal = (
        native_telegram_time(heartbeat, timezone, fmt="dt")
        if heartbeat is not None else
        tr("Нет данных", "No data")
    )
    return (
        tr("✦ <b>Статистика бота</b>\n\n",
           "✦ <b>Bot statistics</b>\n\n")
        + "🤖 <b>@" + escape(bot["username"]) + "</b>\n"
        + tr("Проект: ", "Project: ") + escape(bot["project_name"]) + "\n"
        + tr("Подключение: ", "Connection: ")
        + escape(_status_label(bot["status"])) + "\n"
        + tr("Работа: ", "Runtime: ")
        + escape(_status_label(bot["runtime_state"])) + "\n"
        + tr("📡 Последний сигнал: ", "📡 Last heartbeat: ")
        + last_signal + "\n"
        + tr("🔎 Диагностика: ", "🔎 Diagnosis: ") + escape(condition) + "\n"
        + escape(explanation) + "\n\n"
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

    async def send_platform_home(
        bot: Bot, user, *, period: str, message_id: int | None = None,
    ) -> None:
        report = await read_report(user, period=period)
        rows = _period_rows(period, section="home")
        rows.extend([
            [
                _button(tr("🖥 Мониторинг", "🖥 Monitoring"), f"bots:{period}:0"),
                _button(tr("📊 История", "📊 History"), f"usage:{period}:0"),
            ],
            [
                _button(tr("💼 Финансы", "💼 Finances"), f"merchants:{period}:0"),
                _button(tr("🛡 Проверки", "🛡 Reviews"), f"risks:{period}:0"),
            ],
            [
                InlineKeyboardButton(
                    text=tr("🌐 Язык", "🌐 Language"), callback_data="lng:menu",
                ),
                InlineKeyboardButton(
                    text=tr("👤 Мои боты", "👤 My bots"),
                    callback_data="own:projects",
                ),
            ],
        ])
        await _render_platform_screen(
            bot, chat_id=user.id, text=_summary_text(report),
            reply_markup=_keyboard(rows), message_id=message_id,
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
        message_id = getattr(query.message, "message_id", None)
        try:
            if tokens[1:] == ["home"]:
                await send_platform_home(
                    bot, query.from_user, period="30", message_id=message_id,
                )
            elif len(tokens) == 4 and tokens[1] == "home":
                _, _, period, page_token = tokens
                if period not in PERIODS or page_token != "0":
                    raise ValueError("Некорректный период")
                await send_platform_home(
                    bot, query.from_user, period=period, message_id=message_id,
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
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=_usage_text(report, timezone=config.platform_timezone),
                    reply_markup=_keyboard(rows), message_id=message_id,
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
                if len(chunks) == 1:
                    await _render_platform_screen(
                        bot, chat_id=query.from_user.id, text=chunks[0],
                        reply_markup=_keyboard(rows), message_id=message_id,
                    )
                else:
                    for index, chunk in enumerate(chunks):
                        await bot.send_message(
                            query.from_user.id, chunk, parse_mode="HTML",
                            reply_markup=(
                                _keyboard(rows) if index == len(chunks) - 1 else None
                            ),
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
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=_risk_review_text(report, timezone=config.platform_timezone),
                    reply_markup=_keyboard(rows), message_id=message_id,
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
                        _bot_list_button_label(item),
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
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id, text=_bot_list(report),
                    reply_markup=_keyboard(rows), message_id=message_id,
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
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=_bot_details(
                        report.bots[index], period=period,
                        timezone=config.platform_timezone,
                    ),
                    reply_markup=_keyboard([
                        [_button(tr("⬅️ Мониторинг", "⬅️ Monitoring"), f"bots:{period}:{page}")],
                    ]), message_id=message_id,
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
