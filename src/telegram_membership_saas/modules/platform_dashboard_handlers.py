"""Private ViMi Pay operator menu. No tenant settings or payment mutation.

One Telegram account can navigate between platform and personal bot registry,
but each button performs a fresh server-side role check.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
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
from telegram_membership_saas.modules.owner_setup.rich_admin_ui import send_owner_rich
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
from telegram_membership_saas.modules.platform_saas_billing_service import (
    commercial_release_change,
    commercial_release_status,
    confirm_saas_receipt,
    platform_saas_invoice_report,
)
from telegram_membership_saas.modules.platform_saas_invoice_ui import (
    issued_invoice_classic,
    issued_invoice_rich,
    receipt_fingerprint,
)
from telegram_membership_saas.modules.platform_saas_payment_service import (
    configure_platform_saas_payment,
    platform_saas_payment_details,
)
from telegram_membership_saas.modules.platform_saas_proof_media import show_pending_platform_proof
from telegram_membership_saas.modules.platform_saas_settlement_service import (
    confirm_saas_currency_receipt,
    saas_invoice_credit_history,
    saas_invoice_currency_snapshot,
)
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_catalog_create,
    saas_catalog_currency_set,
    saas_catalog_read,
    saas_catalog_remove,
    saas_catalog_toggle,
    saas_catalog_update,
    saas_v2_configure_method,
    saas_v2_confirm_attempt,
    saas_v2_pending_proofs,
    saas_v2_reject_attempt,
)
from telegram_membership_saas.modules.platform_usage import (
    BOT_EVENT_LABELS,
    EVENT_LABELS,
    platform_usage_report,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)
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
    if runtime == "running" and elapsed is None:
        return (
            tr("⚪ Нет подтверждения работы", "⚪ Runtime not confirmed"),
            tr("Нет подтверждения: время последнего сигнала отсутствует; проверьте Supervisor.",
               "Runtime is not confirmed: no recorded heartbeat; check Supervisor."),
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
    project = str(bot["project_name"]).strip()
    username = str(bot["username"]).strip()
    project_row = (
        tr("Проект: ", "Project: ") + escape(project) + "\n"
        if project.lstrip("@").casefold() != username.lstrip("@").casefold()
        else ""
    )
    # One status, one timestamp. Detailed explanation only when something
    # needs attention; no three different ways of saying "running".
    healthy = condition.startswith("🟢")
    status_row = (
        tr("🟢 Состояние: ", "🟢 Status: ")
        + tr("Работает", "Running") + "\n"
        if healthy else
        tr("Состояние: ", "Status: ") + escape(condition) + "\n"
    )
    explanation_row = (
        tr("⚠️ Причина: ", "⚠️ Reason: ") + escape(explanation) + "\n"
        if not healthy else ""
    )
    connection_row = (
        tr("Подключение: ", "Connection: ")
        + escape(_status_label(str(bot["status"]))) + "\n"
        if bot["status"] in {"disabled", "deleting"} else ""
    )
    return (
        tr("✦ <b>Статистика бота</b>\n\n",
           "✦ <b>Bot statistics</b>\n\n")
        + "🤖 <b>@" + escape(username) + "</b>\n"
        + project_row
        + connection_row
        + status_row
        + tr("📡 Последний сигнал: ", "📡 Last heartbeat: ")
        + last_signal + "\n"
        + explanation_row + "\n"
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


def _saas_invoice_text(report) -> str:
    """One compact owner-only billing screen inside existing Finances."""
    lines = [
        tr("✦ <b>ViMi Pay · Счета</b>", "✦ <b>ViMi Pay · Invoices</b>"),
        tr(
            "Биллинг: <b>активен</b>" if report.enabled
            else "Биллинг: <b>не запущен</b>",
            "Billing: <b>active</b>" if report.enabled
            else "Billing: <b>not activated</b>",
        ),
        tr("Выставленных счетов: ", "Issued invoices: ") + str(report.issued),
        tr("Неоплаченных всего: ", "Total unpaid: ") + str(report.unpaid),
        tr("К оплате сейчас: ", "Due now: ")
        + escape(format(getattr(report, "due_now_rub", report.unpaid_rub), ".2f")) + " ₽",
        tr("Выставлены заранее: ", "Issued ahead of due date: ")
        + str(getattr(report, "upcoming_count", 0)) + " · "
        + escape(format(getattr(report, "upcoming_rub", Decimal("0")), ".2f")) + " ₽",
        tr("Получено: ", "Received: ")
        + escape(format(report.received_rub, ".2f")) + " ₽",
        tr(
            "Ограничения: <b>активны</b>" if report.holds_enabled
            else "Ограничения: <b>выключены</b>",
            "Commercial holds: <b>active</b>" if report.holds_enabled
            else "Commercial holds: <b>off</b>",
        ),
        tr("Защита при проверке чека: до 12 ч.",
           "Pending receipt review protection: up to 12h."),
        tr("Просрочивших владельцев: ", "Owners on hold: ")
        + str(report.held_owners),
        tr("Ожидают решения по чеку: ", "Awaiting receipt decision: ")
        + str(report.grace_owners),
        "",
    ]
    if not report.invoices:
        lines.append(tr(
            "Выставленных счетов пока нет.",
            "No issued invoices yet.",
        ))
    for item in report.invoices:
        due = datetime.fromisoformat(str(item["due_at"]).replace("Z", "+00:00"))
        status = (
            tr("Оплачен", "Paid") if item["status"] == "paid"
            else tr("Выставлен · срок не наступил", "Issued · not yet due")
            if due > datetime.now(ZoneInfo("UTC"))
            else tr("Просрочен", "Overdue")
        )
        lines.extend([
            "• <b>" + escape(str(item["kind"])) + "</b>: "
            + escape(str(item["total_rub"])) + " ₽",
            "  " + status + " · "
            + due.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК"),
        ])
    lines.extend([
        "",
        tr(
            "<i>Подтверждать поступление можно только после "
            "проверки денег по банковским реквизитам.</i>",
            "<i>Confirm payment only after independently verifying "
            "receipt in the bank.</i>",
        ),
    ])
    if report.pages > 1:
        lines.append(
            tr("Страница ", "Page ") + str(report.page + 1)
            + tr(" из ", " of ") + str(report.pages)
        )
    return "\n".join(lines)


def _editor_command(text: str | None) -> str | None:
    """Only a slash command at the beginning of a message is a command."""
    value = (text or "").lstrip()
    if not value.startswith("/"):
        return None
    return value.split(maxsplit=1)[0].split("@", 1)[0].lower()


class PlatformBankNavigationMiddleware(BaseMiddleware):
    """Discard a pending bank edit whenever central UI navigation occurs.

    Installed on the dispatcher, not only on the platform router, so own:/lng:
    callbacks and commands cannot leave a stale payment editor intercepting
    unrelated text. The editor remains an explicit user-initiated one-shot flow.
    """

    def __init__(self) -> None:
        self.pending: set[int] = set()
        self.catalog_pending: dict[int, dict] = {}

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None:
            if isinstance(event, CallbackQuery):
                self.pending.discard(user.id)
                self.catalog_pending.pop(user.id,None)
            elif isinstance(event, Message):
                command = _editor_command(event.text)
                if command is not None and command != "/cancel":
                    self.pending.discard(user.id)
                    self.catalog_pending.pop(user.id,None)
        return await handler(event, data)


class _PlatformBankEntryFilter(BaseFilter):
    def __init__(self, pending: set[int]) -> None:
        self.pending = pending

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.chat.type == "private"
            and message.from_user.id in self.pending
            and message.text is not None
            # Never let a slash command become bank instructions. /cancel
            # deliberately remains the only accepted editor command.
            and (_editor_command(message.text) in (None, "/cancel"))
        )


class _PlatformSaasReviewEntryFilter(BaseFilter):
    def __init__(self, pending: dict[int, tuple[UUID, bool]]) -> None:
        self.pending = pending

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user is not None
            and message.from_user.id in self.pending
            and message.chat.type == "private"
            and message.text is not None
        )


class _PlatformSaasMethodEntryFilter(BaseFilter):
    def __init__(self, pending: dict[int, str]) -> None:
        self.pending = pending

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user is not None
            and message.from_user.id in self.pending
            and message.chat.type == "private"
            and message.text is not None
        )


def _platform_bank_text(details: dict) -> str:
    """Keep bank instructions focused; audit is a sibling invoices screen."""
    configured = bool(details["configured"])
    state = tr("✅ Указаны", "✅ Configured") if configured else tr(
        "⚪ Не указаны", "⚪ Not configured",
    )
    lines = [
        tr("💳 <b>ViMi Pay · Реквизиты</b>",
           "💳 <b>ViMi Pay · Payment instructions</b>"),
        tr("Статус: ", "Status: ") + state,
        "",
    ]
    if configured:
        # Never transform, summarize or log owner-authored banking fields.
        lines.append("<pre>" + escape(str(details["instructions"])) + "</pre>")
    else:
        lines.append(tr(
            "Реквизиты пока не заданы. Уведомления об оплате не рассылаются.",
            "No bank details yet. Payment reminders are not delivered.",
        ))
    lines.extend([
        "",
        tr(
            "<i>Реквизиты нужны только для счетов ViMi Pay. "
            "Их сохранение не запускает биллинг.</i>",
            "<i>These instructions are only for ViMi Pay invoices. "
            "Saving them does not enable billing.</i>",
        ),
    ])
    return "\n".join(lines)


def create_platform_dashboard_router(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings | None = None,
    bank_navigation: PlatformBankNavigationMiddleware | None = None,
) -> Router:
    config = settings or get_settings()
    router = Router(name="platform-dashboard")
    pending_bank_entry = (
        bank_navigation.pending if bank_navigation is not None else set()
    )
    pending_v2_review: dict[int, tuple[UUID, bool]] = {}
    pending_v2_method: dict[int, str] = {}
    pending_catalog: dict[int, dict] = (
        bank_navigation.catalog_pending if bank_navigation is not None else {}
    )

    async def _show_bank_screen(bot: Bot, user, message_id: int | None = None) -> None:
        actor_id = await ensure_telegram_actor(
            factory, telegram_id=user.id, username=user.username,
            first_name=user.first_name,
        )
        details = await platform_saas_payment_details(
            factory, actor_user_id=actor_id, actor_telegram_id=user.id,
            settings=config,
        )
        buttons = [[_button(
            tr("✍️ Изменить реквизиты", "✍️ Edit instructions"), "bankedit",
        )]]
        if details["configured"]:
            buttons.append([_button(
                tr("🗑 Убрать реквизиты", "🗑 Clear instructions"), "bankclearask",
            )])
        buttons.append([_button(
            tr("💱 Реквизиты по валютам", "💱 Currency bank methods"),
            "v2methods",
        )])
        buttons.append([_button(
            tr("⬅️ Счета", "⬅️ Invoices"), "billing:0",
        )])
        await _render_platform_screen(
            bot, chat_id=user.id, text=_platform_bank_text(details),
            reply_markup=_keyboard(buttons), message_id=message_id,
        )

    async def _catalog_actor(user):
        return await ensure_telegram_actor(
            factory, telegram_id=user.id, username=user.username,
            first_name=user.first_name,
        )

    async def _show_catalog(
        bot: Bot, user, message_id: int | None = None,
    ) -> None:
        actor_id = await _catalog_actor(user)
        methods = await saas_catalog_read(
            factory, actor_id=actor_id, actor_telegram_id=user.id,
            settings=config,
        )
        lines = [
            tr("💳 <b>ViMi Pay · Способы оплаты</b>",
               "💳 <b>ViMi Pay · Payment methods</b>"),
            tr("Задайте валюты и реквизиты для оплаты счетов платформы. "
               "Покупатель увидит только включённые способы. "
               "Уже выданные счета и сохранённые чеки не меняются.",
               "Assign currencies and instructions for platform invoices. "
               "Payers see only enabled methods. Existing invoices and "
               "payment proofs remain unchanged."),
        ]
        rows: list[list[InlineKeyboardButton]] = []
        for method in methods:
            codes = ", ".join(method["currencies"])
            lines.append(
                ("✅ " if method["enabled"] else "⏸️ ")
                + escape(method["name"]) + " · " + escape(codes or "—")
            )
            rows.append([_button(
                ("✅ " if method["enabled"] else "⏸️ ")
                + str(method["name"])[:43],
                "pm:" + encode_uuid(UUID(str(method["id"]))),
            )])
        if not methods:
            lines.append(tr("Способы пока не созданы.",
                            "No payment methods configured."))
        rows.extend([
            [_button(tr("➕ Добавить способ", "➕ Add payment method"),
                     "pmnew")],
            [_button(tr("⬅️ Счета", "⬅️ Invoices"), "billing:0")],
        ])
        await _render_platform_screen(
            bot, chat_id=user.id, text="\n\n".join(lines),
            reply_markup=_keyboard(rows), message_id=message_id,
        )

    async def _show_catalog_method(
        bot: Bot, user, method_id: UUID, message_id: int | None = None,
    ) -> None:
        actor_id = await _catalog_actor(user)
        items = await saas_catalog_read(
            factory, actor_id=actor_id, actor_telegram_id=user.id,
            settings=config,
        )
        method = next(
            (item for item in items if item["id"] == str(method_id)), None,
        )
        if method is None:
            raise ValueError("Payment method was removed")
        currencies = set(method["currencies"])
        lines = [
            "💳 <b>" + escape(str(method["name"])) + "</b>",
            tr("Статус: ", "Status: ") + (
                tr("✅ Включён", "✅ Enabled") if method["enabled"]
                else tr("⏸️ Выключен", "⏸️ Disabled")
            ),
            tr("Реквизиты: ", "Instructions: "),
            "<pre>" + escape(str(method["instructions"])) + "</pre>",
            tr("Доступные валюты: ", "Enabled currencies: ")
            + escape(", ".join(sorted(currencies)) or "—"),
        ]
        code = encode_uuid(method_id)
        currows = [_button(
            ("✅ " if cur in currencies else "◯ ") + cur,
            "pmcur:" + code + ":" + cur + ":"
            + ("0" if cur in currencies else "1"),
        ) for cur in ("RUB","USD","EUR","UAH","BYN")]
        rows = [currows[i:i+2] for i in range(0,len(currows),2)]
        rows.extend([
            [_button(tr("✏️ Название", "✏️ Name"), "pmedit:" + code + ":name"),
             _button(tr("✍️ Реквизиты", "✍️ Details"),
                     "pmedit:" + code + ":instructions")],
            [_button(
                tr("⏸️ Отключить", "⏸️ Disable") if method["enabled"]
                else tr("✅ Включить", "✅ Enable"),
                "pmtoggle:" + code + ":" + ("0" if method["enabled"] else "1"),
            )],
            [_button(tr("🗑 Удалить", "🗑 Remove"), "pmdeleteask:" + code)],
            [_button(tr("⬅️ Способы оплаты", "⬅️ Payment methods"), "pm")],
        ])
        await _render_platform_screen(
            bot, chat_id=user.id, text="\n".join(lines),
            reply_markup=_keyboard(rows), message_id=message_id,
        )

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
        if message.from_user is not None:
            pending_bank_entry.discard(message.from_user.id)
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

    @router.message(_PlatformSaasReviewEntryFilter(pending_v2_review))
    async def platform_bank_receipt_reference(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user is None or not is_configured_platform_owner(user.id, config):
            return
        attempt, approve = pending_v2_review[user.id]
        if _editor_command(message.text) == "/cancel":
            pending_v2_review.pop(user.id, None)
            await bot.send_message(user.id, tr(
                "Проверка отменена.", "Review canceled.",
            ))
            return
        if _editor_command(message.text):
            await bot.send_message(user.id, tr(
                "Введите банковский референс или /cancel.",
                "Enter a bank reference or /cancel.",
            ))
            return
        value = message.text.strip()
        # Validate at the Telegram boundary, before an RPC or any bank
        # mutation. Rejection and approval have deliberately different
        # lengths due to their separate database constraints.
        lower, upper = (8, 160) if approve else (5, 500)
        if not lower <= len(value) <= upper:
            await bot.send_message(user.id, tr(
                "⚠️ Для подтверждения введите банковский референс "
                "длиной 8–160 символов." if approve else
                "⚠️ Причина отклонения: от 5 до 500 символов. "
                "Например: «Тестовый чек». Сам счёт не удаляется. "
                "/cancel — отмена.",
                "⚠️ Enter a verified bank reference of 8–160 characters."
                if approve else
                "⚠️ Rejection reason must be 5–500 characters, "
                'for example "Test receipt". The invoice is not deleted. '
                "/cancel to abort.",
            ))
            return
        try:
            actor_id = await ensure_telegram_actor(
                factory, telegram_id=user.id, username=user.username,
                first_name=user.first_name,
            )
            if approve:
                await saas_v2_confirm_attempt(
                    factory, actor_id=actor_id, actor_telegram_id=user.id,
                    attempt_id=attempt, verified_bank_reference=value,
                    settings=config,
                )
            else:
                await saas_v2_reject_attempt(
                    factory, actor_id=actor_id, actor_telegram_id=user.id,
                    attempt_id=attempt, reason=value, settings=config,
                )
        except Exception:
            await bot.send_message(user.id, tr(
                "⚠️ Не удалось подтвердить операцию. Проверьте реальное "
                "поступление, референс и статус счёта. /cancel — отмена.",
                "⚠️ Cannot confirm. Recheck actual bank funds, reference "
                "and invoice status. /cancel to abort.",
            ))
            return
        pending_v2_review.pop(user.id, None)
        await bot.send_message(user.id, tr(
            "✅ Банковское поступление подтверждено." if approve
            else "❌ Заявка отклонена.",
            "✅ Bank payment confirmed." if approve
            else "❌ Payment attempt rejected.",
        ))


    @router.message(_PlatformSaasMethodEntryFilter(pending_catalog))
    async def platform_named_method_entry(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user is None or not is_configured_platform_owner(user.id, config):
            return
        draft = pending_catalog[user.id]
        if _editor_command(message.text) == "/cancel":
            pending_catalog.pop(user.id, None)
            await _show_catalog(bot, user)
            return
        if _editor_command(message.text):
            await bot.send_message(user.id, tr(
                "Введите текст или /cancel.", "Enter text or /cancel.",
            ))
            return
        value = message.text.strip()
        stage = draft["stage"]
        if stage in ("name","edit_name") and not 2 <= len(value) <= 80:
            await bot.send_message(user.id, tr(
                "Название: от 2 до 80 символов.",
                "Name: 2–80 characters.",
            ))
            return
        if stage in ("instructions","edit_instructions") and not (
            8 <= len(value) <= 900
        ):
            await bot.send_message(user.id, tr(
                "Реквизиты: от 8 до 900 символов.",
                "Instructions: 8–900 characters.",
            ))
            return
        if stage == "name":
            draft["name"] = value
            draft["stage"] = "instructions"
            await bot.send_message(user.id, tr(
                "✍️ Отправьте реквизиты и инструкцию оплаты одним сообщением. "
                "/cancel — отмена.",
                "✍️ Send payment details and instructions in one message. "
                "/cancel to abort.",
            ))
            return
        try:
            actor_id = await _catalog_actor(user)
            if stage == "instructions":
                await saas_catalog_create(
                    factory, actor_id=actor_id, actor_telegram_id=user.id,
                    name=draft["name"],instructions=value,
                    currency=draft["currency"],settings=config,
                )
            else:
                all_methods = await saas_catalog_read(
                    factory, actor_id=actor_id, actor_telegram_id=user.id,
                    settings=config,
                )
                target = next((x for x in all_methods
                               if x["id"] == str(draft["method_id"])),None)
                if target is None:
                    raise ValueError("Method no longer exists")
                await saas_catalog_update(
                    factory, actor_id=actor_id, actor_telegram_id=user.id,
                    method_id=draft["method_id"],
                    name=value if stage=="edit_name" else target["name"],
                    instructions=(value if stage=="edit_instructions"
                                  else target["instructions"]),settings=config,
                )
        except (ValueError,RuntimeError):
            await bot.send_message(user.id, tr(
                "⚠️ Не удалось сохранить способ. Проверьте данные и повторите "
                "или /cancel.",
                "⚠️ Could not save the method. Retry or /cancel.",
            ))
            return
        pending_catalog.pop(user.id,None)
        await _show_catalog(bot,user)

    @router.message(_PlatformSaasMethodEntryFilter(pending_v2_method))
    async def platform_v2_bank_method_entry(message: Message, bot: Bot) -> None:
        user = message.from_user
        if user is None or not is_configured_platform_owner(user.id, config):
            return
        currency = pending_v2_method[user.id]
        if _editor_command(message.text) == "/cancel":
            pending_v2_method.pop(user.id, None)
            await _show_bank_screen(bot, user)
            return
        if _editor_command(message.text) not in (None, "/disable"):
            await bot.send_message(user.id, tr(
                "Пришлите реквизиты, /disable или /cancel.",
                "Send bank details, /disable or /cancel.",
            ))
            return
        instructions = None if _editor_command(message.text) == "/disable" else (
            message.text.strip()
        )
        try:
            actor_id = await ensure_telegram_actor(
                factory, telegram_id=user.id, username=user.username,
                first_name=user.first_name,
            )
            await saas_v2_configure_method(
                factory, actor_id=actor_id, actor_telegram_id=user.id,
                currency=currency, instructions=instructions, settings=config,
            )
        except Exception:
            await bot.send_message(user.id, tr(
                "⚠️ Реквизиты отклонены. Проверьте их и повторите.",
                "⚠️ Bank instructions rejected. Please retry.",
            ))
            return
        pending_v2_method.pop(user.id, None)
        await _show_bank_screen(bot, user)

    @router.message(_PlatformBankEntryFilter(pending_bank_entry))
    async def platform_bank_entry(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        user = message.from_user
        if not is_configured_platform_owner(user.id, config):
            pending_bank_entry.discard(user.id)
            return
        if _editor_command(message.text) == "/cancel":
            pending_bank_entry.discard(user.id)
            await _show_bank_screen(bot, user)
            return
        # Do not save slash-prefixed content as bank instructions, even
        # when this callback is invoked without the router's filter.
        if _editor_command(message.text) is not None:
            pending_bank_entry.discard(user.id)
            await bot.send_message(
                user.id,
                tr("Команда не является реквизитами. Откройте редактирование снова.",
                   "Commands are not bank details. Reopen the editor."),
            )
            return
        raw = message.text.strip()
        if len(raw) < 8 or len(raw) > 900:
            await bot.send_message(
                user.id,
                tr("Введите реквизиты длиной 8–900 символов или /cancel.",
                   "Enter 8–900 characters or /cancel."),
            )
            return
        try:
            actor_id = await ensure_telegram_actor(
                factory, telegram_id=user.id, username=user.username,
                first_name=user.first_name,
            )
            await configure_platform_saas_payment(
                factory, actor_user_id=actor_id, actor_telegram_id=user.id,
                instructions=raw, settings=config,
            )
        except (PermissionError, ValueError):
            await bot.send_message(
                user.id, tr("Реквизиты отклонены. Проверьте длину и права.",
                            "Instructions rejected. Check length and permissions."),
            )
            return
        except Exception:
            await bot.send_message(
                user.id, tr("Не удалось сохранить реквизиты. Попробуйте ещё раз.",
                            "Unable to save instructions. Please retry."),
            )
            return
        pending_bank_entry.discard(user.id)
        await _show_bank_screen(bot, user)

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
        # Also cover standalone router tests that bypass dispatcher middleware.
        # All platform navigations leave the form; bankedit reopens it below.
        pending_bank_entry.discard(query.from_user.id)
        pending_v2_review.pop(query.from_user.id, None)
        pending_v2_method.pop(query.from_user.id, None)
        pending_catalog.pop(query.from_user.id, None)
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
                rows.append([_button(
                    tr("🧾 Счета ViMi Pay", "🧾 ViMi Pay invoices"),
                    "billing:0",
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
            elif tokens[1:] == ["bankdetails"]:
                await _show_catalog(
                    bot, query.from_user, message_id=message_id,
                )
            elif tokens[1:] in (
                ["bankedit"], ["bankclearask"], ["bankclearok"],
            ):
                # Stale messages must never mutate a retired bank record.
                # Use the same authoritative named-method editor as new UI.
                await _show_catalog(
                    bot, query.from_user, message_id=message_id,
                )
            elif tokens[1:] == ["pm"]:
                await _show_catalog(
                    bot, query.from_user, message_id=message_id,
                )
            elif tokens[1:] == ["pmnew"]:
                rows = [_button(
                    "💳 " + cur, "pmnew:" + cur,
                ) for cur in ("RUB","USD","EUR","UAH","BYN")]
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=tr(
                        "💱 <b>Выберите первую валюту способа оплаты</b>\n"
                        "Позже можно привязать дополнительные валюты.",
                        "💱 <b>Choose the first payment currency</b>\n"
                        "You can attach more currencies later.",
                    ),
                    reply_markup=_keyboard(
                        [rows[i:i+2] for i in range(0,len(rows),2)]
                        + [[_button(tr("⬅️ Назад", "⬅️ Back"),"pm")]]
                    ), message_id=message_id,
                )
            elif len(tokens)==3 and tokens[1]=="pmnew":
                currency=tokens[2]
                if currency not in ("RUB","USD","EUR","UAH","BYN"):
                    raise ValueError("Invalid catalog currency")
                pending_catalog[query.from_user.id] = {
                    "stage":"name","currency":currency,
                }
                await bot.send_message(query.from_user.id,tr(
                    "✍️ Напишите название способа оплаты, например "
                    "«СБП» или «PayPal». 2–80 символов. /cancel — отмена.",
                    "✍️ Enter a method name, e.g. «Bank transfer» "
                    "or «PayPal». 2–80 characters. /cancel to abort.",
                ))
            elif len(tokens)==3 and tokens[1]=="pm":
                await _show_catalog_method(
                    bot,query.from_user,decode_uuid(tokens[2]),
                    message_id=message_id,
                )
            elif len(tokens)==5 and tokens[1]=="pmcur":
                method_id=decode_uuid(tokens[2])
                currency=tokens[3]
                if tokens[4] not in ("0","1"):
                    raise ValueError("Invalid currency setting")
                owner=await _catalog_actor(query.from_user)
                await saas_catalog_currency_set(
                    factory,actor_id=owner,
                    actor_telegram_id=query.from_user.id,method_id=method_id,
                    currency=currency,enabled=tokens[4]=="1",settings=config,
                )
                await _show_catalog_method(
                    bot,query.from_user,method_id,message_id=message_id,
                )
            elif len(tokens)==4 and tokens[1]=="pmtoggle":
                method_id=decode_uuid(tokens[2])
                if tokens[3] not in ("0","1"):
                    raise ValueError("Invalid method state")
                owner=await _catalog_actor(query.from_user)
                await saas_catalog_toggle(
                    factory,actor_id=owner,actor_telegram_id=query.from_user.id,
                    method_id=method_id,enabled=tokens[3]=="1",settings=config,
                )
                await _show_catalog_method(
                    bot,query.from_user,method_id,message_id=message_id,
                )
            elif len(tokens)==4 and tokens[1]=="pmedit":
                method_id=decode_uuid(tokens[2])
                if tokens[3] not in ("name","instructions"):
                    raise ValueError("Invalid method editor")
                pending_catalog[query.from_user.id]={
                    "stage":"edit_"+tokens[3],"method_id":method_id,
                }
                await bot.send_message(query.from_user.id,tr(
                    "✍️ Введите новое значение или /cancel для отмены.",
                    "✍️ Send the new value or /cancel to abort.",
                ))
            elif len(tokens)==3 and tokens[1]=="pmdeleteask":
                method_id=decode_uuid(tokens[2])
                await _render_platform_screen(
                    bot,chat_id=query.from_user.id,text=tr(
                        "🗑 Удалить способ оплаты? Старые чеки и попытки "
                        "оплаты сохранят свои реквизиты.",
                        "🗑 Remove this payment method? Existing "
                        "payment attempts keep their original instructions.",
                    ),
                    reply_markup=_keyboard([
                        [_button(tr("🗑 Подтвердить","🗑 Confirm"),
                                 "pmdelete:"+tokens[2])],
                        [_button(tr("⬅️ Отмена","⬅️ Cancel"),
                                 "pm:"+tokens[2])],
                    ]),message_id=message_id,
                )
            elif len(tokens)==3 and tokens[1]=="pmdelete":
                owner=await _catalog_actor(query.from_user)
                await saas_catalog_remove(
                    factory,actor_id=owner,
                    actor_telegram_id=query.from_user.id,
                    method_id=decode_uuid(tokens[2]),settings=config,
                )
                await _show_catalog(
                    bot,query.from_user,message_id=message_id,
                )
            elif tokens[1:] == ["v2methods"] or (
                len(tokens) == 3 and tokens[1] == "v2method"
            ):
                # Old currency-only keyboards remain navigable but cannot
                # create a competing source of payment instructions.
                await _show_catalog(
                    bot, query.from_user, message_id=message_id,
                )
            elif len(tokens) == 3 and tokens[1] == "v2proofs":
                page_token = tokens[2]
                if (not page_token.isascii() or not page_token.isdecimal()
                        or int(page_token) > 100000):
                    raise ValueError("Invalid bank proof review page")
                page = int(page_token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                result = await saas_v2_pending_proofs(
                    factory, actor_id=actor_id,
                    actor_telegram_id=query.from_user.id, page=page,
                    settings=config,
                )
                rows = []
                lines = [
                    tr("🧾 <b>ViMi Pay · Проверка чеков</b>",
                       "🧾 <b>ViMi Pay · Payment proof review</b>"),
                    tr("Сверяйте <b>фактическое поступление</b> в банке, "
                       "сумму и референс. Фото само по себе не оплата.",
                       "Verify <b>actual received funds</b>, amount and "
                       "bank reference. A screenshot is not payment."),
                ]
                items = result["items"]
                if not items:
                    lines.append(tr("Новых заявок нет.", "No new submissions."))
                for index, item in enumerate(items, start=1):
                    attempt_id = UUID(str(item["id"]))
                    token = encode_uuid(attempt_id)
                    receipt_time = item.get("submitted_at")
                    stamp = ""
                    if receipt_time:
                        try:
                            submitted = datetime.fromisoformat(
                                str(receipt_time).replace("Z", "+00:00"),
                            ).astimezone(ZoneInfo(config.platform_timezone))
                            stamp = submitted.strftime("%d.%m.%Y %H:%M")
                        except ValueError:
                            pass
                    lines.append(
                        "\n" + tr("🧾 <b>Чек №", "🧾 <b>Receipt #")
                        + str(page * 8 + index) + "</b>\n"
                        + tr("Сумма: ", "Amount: ") + "<b>"
                        + escape(str(item["amount"]) + " " + str(item["currency"]))
                        + "</b>"
                        + ("\n" + tr("Получен: ", "Received: ")
                           + escape(stamp) if stamp else "")
                    )
                    rows.append([
                        _button(tr("📎 Чек", "📎 Receipt"),
                                "v2proof:" + token + ":" + page_token),
                        _button(tr("✅ Подтвердить", "✅ Approve"),
                                "v2review:ok:" + token + ":" + page_token),
                        _button(tr("❌ Отклонить", "❌ Reject"),
                                "v2review:no:" + token + ":" + page_token),
                    ])
                nav = []
                if page:
                    nav.append(_button("⬅️", "v2proofs:" + str(page - 1)))
                if len(items) == 8:
                    nav.append(_button("➡️", "v2proofs:" + str(page + 1)))
                if nav:
                    rows.append(nav)
                # The queue is queried afresh on every callback. Show when
                # that check happened even if the items haven't changed;
                # Telegram otherwise reports "message is not modified".
                checked = datetime.now(
                    ZoneInfo(config.platform_timezone),
                ).strftime("%H:%M:%S %Z")
                lines.append(
                    "\n" + tr("⏱ Проверено: ", "⏱ Checked: ")
                    + "<code>" + escape(checked) + "</code>"
                )
                rows.append([_button(
                    tr("🔄 Обновить", "🔄 Refresh"), "v2proofs:" + page_token,
                )])
                rows.append([_button(
                    tr("⬅️ Счета", "⬅️ Invoices"), "billing:0",
                )])
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text="\n".join(lines), reply_markup=_keyboard(rows),
                    message_id=message_id,
                )
                # Even two taps in the same second yield identical text.
                # A non-alert callback toast confirms the fresh DB check.
                await query.answer(tr(
                    "✅ Список проверен", "✅ List checked",
                ))
                return
            elif len(tokens) == 4 and tokens[1] == "v2proof":
                # A receipt belongs to the bot that received it. The
                # central bot must re-upload bytes, never another bot's
                # file_id. No persistent receipt media or new debug menu.
                _, _, attempt_token, page_token = tokens
                if (not page_token.isascii() or not page_token.isdecimal()
                        or int(page_token) > 100000):
                    raise ValueError("Invalid receipt page")
                attempt_id = decode_uuid(attempt_token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                await query.answer()
                try:
                    await show_pending_platform_proof(
                        factory, central_bot=bot,
                        operator_id=query.from_user.id, actor_id=actor_id,
                        attempt_id=attempt_id, settings=config,
                        locale_is_english=current_language() == "en",
                    )
                except Exception:
                    # Never log bot-token/API URLs or original receipt data.
                    logging.getLogger(__name__).warning(
                        "ViMi Pay pending receipt cannot be fetched"
                    )
                    await bot.send_message(
                        query.from_user.id,
                        tr("⚠️ Чек недоступен через исходного бота. "
                           "Проверьте подключение бота или запросите "
                           "повторную отправку чека. Платёж не подтверждён.",
                           "⚠️ The source bot cannot deliver this receipt. "
                           "Check its connection or request another proof. "
                           "No payment was approved."),
                    )
                return
            elif len(tokens) == 5 and tokens[1] == "v2review":
                _, _, decision, attempt_token, page_token = tokens
                if decision not in ("ok", "no"):
                    raise ValueError("Invalid bank review decision")
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid review page")
                attempt_id = decode_uuid(attempt_token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                queue = await saas_v2_pending_proofs(
                    factory, actor_id=actor_id,
                    actor_telegram_id=query.from_user.id,
                    page=int(page_token), settings=config,
                )
                if not any(str(item["id"]) == str(attempt_id)
                           for item in queue["items"]):
                    raise ValueError("Original payment attempt no longer pending")
                pending_v2_review[query.from_user.id] = (
                    attempt_id, decision == "ok",
                )
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=tr(
                        "✅ <b>Подтверждение банковского поступления</b>\n"
                        "Только после независимой проверки банка введите "
                        "уникальный референс поступившего перевода "
                        "(8–160 символов). /cancel — отменить.",
                        "✅ <b>Verify actual bank receipt</b>\n"
                        "Only after independently checking the bank, "
                        "enter the unique received transfer reference "
                        "(8–160 characters). /cancel to abort.",
                    ) if decision == "ok" else tr(
                        "❌ <b>Отклонение чека</b>\n"
                        "Причина: 5–500 символов. Чек будет отклонён, "
                        "но сам счёт останется неоплаченным. "
                        "/cancel — отмена.",
                        "❌ <b>Reject receipt</b>\n"
                        "Reason: 5–500 characters. The receipt is rejected, "
                        "but the invoice remains unpaid. "
                        "/cancel to abort.",
                    ),
                    reply_markup=_keyboard([[_button(
                        tr("⬅️ Назад", "⬅️ Back"),
                        "v2proofs:" + page_token,
                    )]]), message_id=message_id,
                )
            elif tokens[1:] == ["commercial"]:
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                status = await commercial_release_status(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, settings=config,
                )
                bot_count = int(status["registered_bots"])
                other_count = int(status["nonoperator_owners"])
                enabled = bool(status["billing_enabled"])
                lines = [
                    tr("💰 <b>ViMi Pay · Монетизация</b>",
                       "💰 <b>ViMi Pay · Monetization</b>"),
                    tr("Статус: ", "Status: ") + (
                        tr("<b>Работает</b>", "<b>Active</b>")
                        if enabled else
                        tr("<b>Выключено</b>", "<b>Disabled</b>")
                    ),
                    tr("Подключено ботов: ", "Connected bots: ")
                    + str(bot_count),
                    tr("Боты владельца платформы: ", "Platform-owner bots: ")
                    + str(status["operator_bots"])
                    + tr(" (без начислений)", " (exempt)"),
                    tr("Боты других аккаунтов: ", "Other-owner bots: ")
                    + str(status["nonoperator_bots"]),
                    tr("Других аккаунтов: ", "Other owner accounts: ")
                    + str(other_count),
                    tr("Непроверенных освобождений: ",
                       "Unverified creator exemptions: ")
                    + str(status["operator_exemption_pending"]),
                    tr("Счета к оплате: ", "Outstanding invoices: ")
                    + str(status["open_invoices"]),
                    tr("Ограничения: ", "New-commerce holds: ")
                    + (tr("включены", "enabled")
                       if status["holds_enabled"] else
                       tr("выключены", "off")),
                    tr("72 часа бесплатно, затем 150 ₽/$3 за бот и 5% "
                       "от подтверждённых продаж.",
                       "72h free, then RUB 150/$3 per bot and 5% "
                       "of verified sales."),
                    tr("Покупки и действующий доступ не отключаются.",
                       "Existing paid access and purchases stay active."),
                ]
                if status["billing_from"]:
                    lines.append(tr("Начало: ", "Effective: ")
                                 + escape(str(status["billing_from"])))
                rows = [[_button(
                    tr("⏹ Отключить монетизацию",
                       "⏹ Disable monetization")
                    if enabled else
                    tr("💰 Включить монетизацию",
                       "💰 Enable monetization"),
                    "commercialconfirm:" + (
                        "stop" if enabled else "arm"
                    ) + ":" + str(bot_count) + ":" + str(other_count),
                )], [_button(
                    tr("⬅️ Счета", "⬅️ Invoices"), "billing:0",
                )]]
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text="\n".join(lines), reply_markup=_keyboard(rows),
                    message_id=message_id,
                )
            elif len(tokens) == 5 and tokens[1] == "commercialconfirm":
                _, _, action, bot_count_str, owner_count_str = tokens
                if (action not in ("arm", "stop")
                    or not bot_count_str.isdecimal()
                    or not owner_count_str.isdecimal()):
                    raise ValueError("Invalid monetization request")
                body = (
                    tr("⚠️ <b>Запустить реальную монетизацию?</b>\n"
                       "Без начислений задним числом; существующие другие "
                       "аккаунты получат оставшийся/уже истёкший 72-часовой "
                       "период. Ограничения новых продаж только после "
                       "7 дней просрочки. Оплаченный доступ сохраняется.",
                       "⚠️ <b>Activate real monetization?</b>\n"
                       "No retroactive charges. Existing other accounts "
                       "keep their 72h trial credit. New checkouts may be "
                       "held after seven overdue days; paid access remains.")
                    if action == "arm" else
                    tr("⏹ <b>Выключить коммерческие начисления?</b>\n"
                       "История платежей и доступ покупателей сохраняются.",
                       "⏹ <b>Disable commercial billing?</b>\n"
                       "Payment history and buyer access are retained.")
                )
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id, text=body,
                    reply_markup=_keyboard([
                        [_button(
                            tr("✅ Подтвердить", "✅ Confirm"),
                            "commercialapply:" + action + ":"
                            + bot_count_str + ":" + owner_count_str,
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "commercial")],
                    ]), message_id=message_id,
                )
            elif len(tokens) == 5 and tokens[1] == "commercialapply":
                _, _, action, bot_count_str, owner_count_str = tokens
                if (action not in ("arm", "stop")
                    or not bot_count_str.isdecimal()
                    or not owner_count_str.isdecimal()):
                    raise ValueError("Invalid monetization confirmation")
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                await commercial_release_change(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id,
                    action=action, expected_bots=int(bot_count_str),
                    expected_other_owners=int(owner_count_str),
                    settings=config,
                )
                status = await commercial_release_status(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, settings=config,
                )
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=tr("💰 Монетизация: ", "💰 Monetization: ")
                    + (tr("<b>активирована</b>", "<b>activated</b>")
                       if status["billing_enabled"] else
                       tr("<b>выключена</b>", "<b>disabled</b>"))
                    + tr("\nДата начала: ", "\nEffective from: ")
                    + escape(str(status["billing_from"] or "—")),
                    reply_markup=_keyboard([[_button(
                        tr("⬅️ Монетизация", "⬅️ Monetization"),
                        "commercial",
                    )]]), message_id=message_id,
                )
            elif len(tokens) == 3 and tokens[1] == "billing":
                page_token = tokens[2]
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid billing page")
                page = int(page_token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_saas_invoice_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, page=page,
                    settings=config,
                )
                rows = []
                for item in report.invoices:
                    name = str(item["kind"])
                    label = ("✅ " if item["status"] == "paid" else "💳 ")
                    label += (
                        name + " · " + str(item["total_rub"]) + " ₽"
                        + " #" + str(item["id"])[:8]
                    )
                    rows.append([_button(
                        label[:55],
                        "billcheck:" + encode_uuid(UUID(item["id"])) + ":" + str(page),
                    )])
                nav = []
                if page > 0:
                    nav.append(_button("⬅️", f"billing:{page - 1}"))
                if page+1 < report.pages:
                    nav.append(_button("➡️", f"billing:{page + 1}"))
                if nav:
                    rows.append(nav)
                rows.append([_button(
                    tr("💰 Монетизация", "💰 Monetization"),
                    "commercial",
                )])
                rows.append([_button(
                    tr("🧾 Чеки на проверку", "🧾 Pending payment proofs"),
                    "v2proofs:0",
                )])
                rows.append([_button(
                    tr("💳 Способы оплаты ViMi Pay",
                       "💳 ViMi Pay payment methods"),
                    "bankdetails",
                )])
                rows.append([_button(
                    tr("⬅️ Финансы", "⬅️ Finances"), "merchants:30:0",
                )])
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=_saas_invoice_text(report),
                    reply_markup=_keyboard(rows), message_id=message_id,
                )
            elif len(tokens) == 4 and tokens[1] == "billcheck":
                _, _, token, page_token = tokens
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid invoice page")
                page = int(page_token)
                invoice_id = decode_uuid(token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_saas_invoice_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, page=page,
                    settings=config,
                )
                item = next((
                    row for row in report.invoices
                    if row["id"] == str(invoice_id)
                ), None)
                if item is None:
                    raise ValueError("Invoice no longer on this page")
                snapshot = await saas_invoice_currency_snapshot(
                    factory, actor_user_id=actor_id, invoice_id=invoice_id,
                )
                credits = await saas_invoice_credit_history(
                    factory, actor_user_id=actor_id, invoice_id=invoice_id,
                )
                is_paid = item["status"] == "paid"
                rows = []
                if not is_paid:
                    label = tr("✅ Подтвердить поступление",
                               "✅ Confirm bank receipt")
                    if snapshot.get("locked"):
                        label += " · " + str(snapshot["amount"]) + " " + str(
                            snapshot["currency"]
                        )
                    rows.append([_button(
                        label[:64], "billcommit:" + token + ":" + str(page)
                        + ":" + receipt_fingerprint(invoice_id, item, snapshot),
                    )])
                rows.append([_button(
                    tr("⬅️ К счетам", "⬅️ Invoices"), f"billing:{page}",
                )])
                context = (
                    "<p>" + tr("Бизнес: ", "Merchant: ")
                    + "<code>" + escape(str(item["merchant"])) + "</code></p>"
                    + "<p>" + tr("Владелец: ", "Owner: ")
                    + "<code>" + escape(str(item["owner"])) + "</code></p>"
                )
                await send_owner_rich(
                    bot, chat_id=query.from_user.id,
                    rich_html=issued_invoice_rich(item, snapshot, credits) + context,
                    classic_html=issued_invoice_classic(item, snapshot, credits)
                    + "\n" + context.replace("<p>", "").replace("</p>", "\n")
                    + tr(
                        "\nПодтверждайте только фактическое поступление по банку.",
                        "\nConfirm only after independently checking the bank receipt.",
                    ),
                    reply_markup=_keyboard(rows),
                )
            elif len(tokens) == 5 and tokens[1] == "billcommit":
                _, _, token, page_token, reviewed_token = tokens
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid invoice page")
                page = int(page_token)
                invoice_id = decode_uuid(token)
                actor_id = await ensure_telegram_actor(
                    factory, telegram_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                report = await platform_saas_invoice_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, page=page,
                    settings=config,
                )
                item = next((
                    x for x in report.invoices if x["id"] == str(invoice_id)
                ), None)
                if item is None:
                    raise ValueError("Invoice no longer on this page")
                snapshot = await saas_invoice_currency_snapshot(
                    factory, actor_user_id=actor_id, invoice_id=invoice_id,
                )
                if (
                    item["status"] != "unpaid"
                    or reviewed_token != receipt_fingerprint(
                        invoice_id, item, snapshot,
                    )
                ):
                    await _render_platform_screen(
                        bot, chat_id=query.from_user.id,
                        text=tr(
                            "⚠️ Условия счёта изменились с момента просмотра. "
                            "Проверьте валюту и сумму заново, прежде чем "
                            "подтверждать поступление.",
                            "⚠️ Invoice terms changed since the last view. "
                            "Review the currency and amount again before "
                            "confirming bank receipt.",
                        ),
                        reply_markup=_keyboard([[_button(
                            tr("⬅️ Обновить счёт", "⬅️ Refresh invoice"),
                            "billcheck:" + token + ":" + str(page),
                        )]]), message_id=message_id,
                    )
                    await query.answer()
                    return
                if snapshot.get("locked"):
                    await confirm_saas_currency_receipt(
                        factory, actor_user_id=actor_id,
                        actor_telegram_id=query.from_user.id,
                        invoice_id=invoice_id, settings=config,
                        currency=str(snapshot["currency"]),
                        amount=Decimal(str(snapshot["amount"])),
                    )
                else:
                    # Legacy invoices without an explicit locked quote remain
                    # payable only through the original RUB receipt path.
                    await confirm_saas_receipt(
                        factory, actor_user_id=actor_id,
                        actor_telegram_id=query.from_user.id,
                        invoice_id=invoice_id, settings=config,
                    )
                refreshed = await platform_saas_invoice_report(
                    factory, actor_user_id=actor_id,
                    actor_telegram_id=query.from_user.id, page=page,
                    settings=config,
                )
                await _render_platform_screen(
                    bot, chat_id=query.from_user.id,
                    text=_saas_invoice_text(refreshed),
                    reply_markup=_keyboard([[
                        _button(tr("⬅️ К счетам", "⬅️ Invoices"),
                                f"billing:{page}"),
                    ]]), message_id=message_id,
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
