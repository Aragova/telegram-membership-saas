"""Compact RU/EN dashboard: two 2-cell rows and verified Telegram finances.

Render-only: readiness comes from setup state, owner from Telegram's creator,
and the bot's Stars balance from the official Bot API. No invented channel
balance, available withdrawal, or beneficiary identity.
"""
from __future__ import annotations

import re
from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
    ChannelFinance,
    DashboardFinances,
)

EMOJI_GAP = "\u2002"


def _status_text(label: str, ready: bool) -> str:
    return ("✅" if ready else "▫️") + EMOJI_GAP + escape(label)


def _status_cell(label: str, ready: bool) -> str:
    # The same ON / OFF vocabulary is used for Russian and English.
    state = ("🟢" if ready else "⚪") + EMOJI_GAP + ("ON" if ready else "OFF")
    return "<td><b>" + _status_text(label, ready) + "</b><br>" + state + "</td>"


def _recipient(value: str) -> str:
    return {
        "bot": tr("🤖 Бот", "🤖 Bot"),
        "channel": tr("📢 Канал", "📢 Channel"),
        "mixed": tr("📢 Канал и 🤖 бот", "📢 Channel and 🤖 bot"),
        "unconfigured": tr("Не настроено", "Not configured"),
    }.get(value, tr("Не проверено", "Not verified"))


def _owner(channel: ChannelFinance) -> str:
    """Only expose real Telegram identity; never turn arbitrary text into a URL."""
    if channel.owner_deleted:
        return (
            tr("⚠️ Удалённый аккаунт", "⚠️ Deleted account")
            + (" · " + escape(channel.owner_label) if channel.owner_label else "")
        )
    username = channel.owner_username
    if username and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        # An official @username resolves directly to a Telegram profile.
        name = escape(username)
        return f'<a href="https://t.me/{name}">@{name}</a>'
    user_id = channel.owner_user_id
    if user_id is not None and user_id > 0:
        # Telegram's user mention deep link works without a public username
        # when privacy and Telegram client permissions permit it.
        display = (
            channel.owner_label.split(" · ID ", 1)[0]
            if channel.owner_label else ""
        )
        if not display or display.startswith("ID "):
            display = tr("Профиль Telegram", "Telegram profile")
        return f'<a href="tg://user?id={user_id}">{escape(display)}</a>'
    return escape(channel.owner_label or tr(
        "Не удалось определить", "Cannot determine",
    ))


def _channel_rows(channel: ChannelFinance) -> tuple[tuple[str, str], ...]:
    """Channel Stars and owner share one row, without pretending bot Stars
    are channel revenue. Mixed / bot routes retain an explicit beneficiary.
    """
    owner = _owner(channel)
    if channel.stars_recipient == "channel":
        return ((
            tr("👑 Владелец · ⭐ каналу", "👑 Owner · ⭐ to channel"),
            owner,
        ),)
    return (
        (tr("👑 Владелец канала", "👑 Channel owner"), owner),
        (tr("💳 Получатель звёзд", "💳 Stars recipient"),
         _recipient(channel.stars_recipient)),
    )



def _panel_data(
    *, readiness: tuple[tuple[str, bool], ...],
    channels_count: int | None, active_plans_count: int | None,
    bot_status: str | None, payment_ready: bool | None,
) -> tuple[tuple[str, str], ...]:
    """Four actual read-only checks, never the legacy ON/OFF indicators."""
    channel = (
        channels_count if channels_count is not None
        else int(bool(readiness and readiness[0][1]))
    )
    plans = (
        active_plans_count if active_plans_count is not None
        else int(len(readiness) > 1 and readiness[1][1])
    )
    payment = (
        payment_ready if payment_ready is not None
        else bool(len(readiness) > 2 and readiness[2][1])
    )
    status = bot_status or (
        tr("🟢 Работает", "🟢 Running")
        if len(readiness) > 3 and readiness[3][1]
        else tr("⚪ Не запущен", "⚪ Not running")
    )
    return (
        (tr("Состояние", "Status"), status),
        (tr("Каналы", "Channels"), tr(
            f"{channel} подключён" if channel == 1 else f"{channel} подключено",
            f"{channel} connected",
        )),
        (tr("Активные тарифы", "Active plans"), str(plans)),
        (tr("Оплата покупателей", "Buyer payments"),
         tr("✅ Настроена", "✅ Configured") if payment else
         tr("⚪ Не настроена", "⚪ Not configured")),
    )


def _service_data(
    service_currency: str | None, service_debt: str | None,
) -> tuple[str, str]:
    rate = {
        "RUB": tr("🇷🇺 150 ₽/мес. + 5%", "🇷🇺 RUB 150/month + 5%"),
        "USD": tr("🇺🇸 $5/мес. + 5%", "🇺🇸 USD 5/month + 5%"),
    }.get(service_currency, tr("Не определена", "Not set"))
    return rate, service_debt or tr("Недоступна", "Unavailable")


def classic_dashboard(
    *, project_name: str, timezone: str, trial_banner: str,
    readiness: tuple[tuple[str, bool], ...], sales_status: str,
    finances: DashboardFinances, exempt: bool = False,
    channels_count: int | None = None,
    active_plans_count: int | None = None,
    bot_status: str | None = None,
    payment_ready: bool | None = None,
    service_currency: str | None = None,
    service_debt: str | None = None,
) -> str:
    rows = _panel_data(
        readiness=readiness, channels_count=channels_count,
        active_plans_count=active_plans_count, bot_status=bot_status,
        payment_ready=payment_ready,
    )
    rate, debt = _service_data(service_currency, service_debt)
    lines = [
        tr("✦ <b>Панель администратора</b>", "✦ <b>Admin dashboard</b>")
        + (" · 👑 ViMi Pay PRO" if exempt else ""),
        "<b>" + escape(project_name) + "</b> 🌍 " + escape(timezone),
        "",
    ]
    lines.extend(escape(k) + ": <b>" + escape(v) + "</b>" for k, v in rows)
    if sales_status and not sales_status.startswith(("✅ Продажи", "✅ Sales")):
        lines.append(sales_status)
    lines.extend([
        "",
        tr("🧾 <b>Обслуживание ViMi Pay</b>", "🧾 <b>ViMi Pay service</b>"),
        trial_banner or tr("⏳ Проверяется", "⏳ Checking status"),
        tr("Тариф: ", "Service rate: ") + escape(rate),
        tr("Задолженность: ", "Amount due: ") + escape(debt),
        "",
        tr("⭐ <b>Финансы Telegram</b>", "⭐ <b>Telegram finances</b>"),
        tr("🤖 Баланс бота: ", "🤖 Bot balance: ") + (
            escape(finances.bot_stars) + " ⭐" if finances.bot_stars is not None
            else tr("Недоступен", "Unavailable")
        ),
    ])
    return "\n".join(lines)


def rich_dashboard(
    *, project_name: str, timezone: str, trial_banner: str,
    readiness: tuple[tuple[str, bool], ...], sales_status: str,
    finances: DashboardFinances, exempt: bool = False,
    channels_count: int | None = None,
    active_plans_count: int | None = None,
    bot_status: str | None = None,
    payment_ready: bool | None = None,
    service_currency: str | None = None,
    service_debt: str | None = None,
) -> str:
    """Approved 4-row panel, ViMi Pay service, bot Stars, embedded 12 actions."""
    rows = _panel_data(
        readiness=readiness, channels_count=channels_count,
        active_plans_count=active_plans_count, bot_status=bot_status,
        payment_ready=payment_ready,
    )
    rate, debt = _service_data(service_currency, service_debt)
    parts = [
        "<h3>" + tr("✦ Панель администратора", "✦ Admin dashboard")
        + (" · 👑 ViMi Pay PRO" if exempt else "") + "</h3>",
        "<p><b>" + escape(project_name) + "</b> 🌍 "
        + escape(timezone) + "</p>",
        "<table bordered compact>",
    ]
    for label, value in rows:
        parts.append("<tr><th>" + escape(label) + "</th><td><b>"
                     + escape(value) + "</b></td></tr>")
    parts.append("</table>")
    if sales_status and not sales_status.startswith(("✅ Продажи", "✅ Sales")):
        parts.append("<p>" + escape(sales_status) + "</p>")
    parts.extend([
        "<h4>" + tr("🧾 Обслуживание ViMi Pay", "🧾 ViMi Pay service") + "</h4>",
        "<p><b>" + escape(trial_banner or tr(
            "⏳ Проверяется", "⏳ Checking status",
        )) + "</b></p>",
        "<table bordered compact>",
        "<tr><th>" + tr("Тариф", "Service rate")
        + "</th><td><b>" + escape(rate) + "</b></td></tr>",
        "<tr><th>" + tr("Задолженность", "Amount due")
        + "</th><td><b>" + escape(debt) + "</b></td></tr>",
        "</table>",
        "<h4>" + tr("⭐ Финансы Telegram", "⭐ Telegram finances") + "</h4>",
        "<table bordered compact>",
        "<tr><th>" + tr("🤖 Баланс бота", "🤖 Bot balance")
        + "</th><td><b>" + (
            escape(finances.bot_stars) + " ⭐" if finances.bot_stars is not None
            else tr("Недоступен", "Unavailable")
        ) + "</b></td></tr>",
        "</table>",
    ])
    # Renderer replaces this marker with native in-message button rows.
    from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER
    parts.append("<h4>" + tr("⚡ Управление ботом", "⚡ Bot management") + "</h4>")
    parts.append(RICH_ACTIONS_MARKER)
    return "\n".join(parts)
