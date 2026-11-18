"""Compact, localized dashboard: two status rows and one Telegram finance card.

Rendering only. Every readiness check comes from the existing live setup
overview; missing balances/ownership are never synthesized as successes.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
    ChannelFinance,
    DashboardFinances,
)


def _status_text(label: str, ready: bool) -> str:
    return ("✅ " if ready else "▫️ ") + label


def _status_cell(label: str, ready: bool, *, bot_runtime: bool) -> str:
    result = (
        tr("🟢 Активен", "🟢 Active")
        if bot_runtime and ready else
        tr("🟢 Подключено", "🟢 Connected") if ready else
        tr("⚪ Не готов", "⚪ Not ready")
    )
    return "<td><b>" + _status_text(escape(label), ready) + "</b><br>" + result + "</td>"


def _recipient(value: str) -> str:
    return {
        "bot": tr("🤖 Бот", "🤖 Bot"),
        "channel": tr("📢 Канал", "📢 Channel"),
        "mixed": tr("📢 Канал и 🤖 бот", "📢 Channel and 🤖 bot"),
        "unconfigured": tr("Не настроено", "Not configured"),
    }.get(value, tr("Не проверено", "Not verified"))


def _owner(channel: ChannelFinance) -> str:
    if channel.owner_deleted:
        return (
            tr("⚠️ Удалённый аккаунт", "⚠️ Deleted account")
            + (" · " + channel.owner_label if channel.owner_label else "")
        )
    return channel.owner_label or tr("Не удалось определить", "Cannot determine")


def classic_dashboard(
    *, project_name: str, timezone: str, trial_banner: str,
    readiness: tuple[tuple[str, bool], ...], sales_status: str,
    finances: DashboardFinances, exempt: bool = False,
) -> str:
    statuses = (
        " · ".join(_status_text(label, ready) for label, ready in readiness[:3])
        + "\n" + " · ".join(
            _status_text(label, ready) for label, ready in readiness[3:5]
        )
    )
    items = [
        tr("✦ <b>Панель администратора</b>", "✦ <b>Admin dashboard</b>")
        + (" · 👑 ViMi Pay PRO" if exempt else ""),
        "<b>" + escape(project_name) + "</b>",
        "🌍 " + escape(timezone),
    ]
    if trial_banner:
        items.extend(["", trial_banner])
    items.extend(["", statuses, "", sales_status, ""])
    items.extend([
        tr("⭐ <b>Финансы Telegram</b>", "⭐ <b>Telegram finances</b>"),
        tr("🤖 Баланс бота: ", "🤖 Bot balance: ")
        + (escape(finances.bot_stars) + " ⭐" if finances.bot_stars is not None
           else tr("Недоступен", "Unavailable")),
    ])
    for channel in finances.channels:
        if len(finances.channels) > 1 or finances.extra_channels:
            items.append("📢 <b>" + escape(channel.name) + "</b>")
        items.extend([
            tr("📢 Баланс канала: недоступен через Bot API",
               "📢 Channel balance: unavailable through Bot API"),
            tr("💸 Вывод: не проверен", "💸 Withdrawal: not verified"),
            tr("👑 Владелец канала: ", "👑 Channel owner: ")
            + escape(_owner(channel)),
            tr("💳 Получатель звёзд: ", "💳 Stars recipient: ")
            + _recipient(channel.stars_recipient),
        ])
    if not finances.channels:
        items.append(tr("📢 Нет подключённых каналов",
                        "📢 No connected channels"))
    if finances.extra_channels:
        items.append(tr(
            f"📢 Ещё каналов: {finances.extra_channels} · откройте «Каналы».",
            f"📢 {finances.extra_channels} more channels · open Channels.",
        ))
    return "\n".join(items)


def rich_dashboard(
    *, project_name: str, timezone: str, trial_banner: str,
    readiness: tuple[tuple[str, bool], ...], sales_status: str,
    finances: DashboardFinances, exempt: bool = False,
) -> str:
    """Telegram Rich HTML supports tables, not custom CSS or invented badges."""
    parts = [
        "<h3>" + tr("✦ Панель администратора", "✦ Admin dashboard")
        + (" · 👑 ViMi Pay PRO" if exempt else "") + "</h3>",
        "<p><b>" + escape(project_name) + "</b><br>"
        + "🌍 " + escape(timezone) + "</p>",
    ]
    if trial_banner:
        parts.append("<p><b>" + trial_banner.replace("\n", "<br>") + "</b></p>")
    # Actual two-row status layout, 3 compact cells and then 2 wide cells.
    for group in (readiness[:3], readiness[3:5]):
        parts.append("<table bordered compact><tr>")
        parts.extend(
            _status_cell(label, ready, bot_runtime=(index == 4))
            for index, (label, ready) in enumerate(
                group, start=0 if len(group) == 3 else 3,
            )
        )
        parts.append("</tr></table>")
    parts.extend([
        "<p>" + sales_status.replace("\n", "<br>") + "</p>",
        "<h4>" + tr("⭐ Финансы Telegram", "⭐ Telegram finances") + "</h4>",
        "<table bordered compact>",
        "<tr><th>" + tr("🤖 Баланс бота", "🤖 Bot balance")
        + "</th><td><b>"
        + (escape(finances.bot_stars) + " ⭐" if finances.bot_stars is not None
           else tr("Недоступен", "Unavailable"))
        + "</b></td></tr>",
        "</table>",
    ])
    if not finances.channels:
        parts.append("<p>" + tr(
            "📢 Подключённых каналов нет",
            "📢 No connected channels",
        ) + "</p>")
    for channel in finances.channels:
        if len(finances.channels) > 1 or finances.extra_channels:
            parts.append("<p><b>📢 " + escape(channel.name) + "</b></p>")
        parts.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("📢 Баланс канала", "📢 Channel balance")
            + "</th><td>" + tr("Недоступен через Bot API",
                               "Unavailable through Bot API") + "</td></tr>",
            "<tr><th>" + tr("💸 Вывод", "💸 Withdrawal")
            + "</th><td>" + tr("⚪ Не проверен", "⚪ Not verified") + "</td></tr>",
            "<tr><th>" + tr("👑 Владелец канала", "👑 Channel owner")
            + "</th><td>" + escape(_owner(channel)) + "</td></tr>",
            "<tr><th>" + tr("💳 Получатель звёзд", "💳 Stars recipient")
            + "</th><td>" + _recipient(channel.stars_recipient) + "</td></tr>",
            "</table>",
        ])
    if finances.extra_channels:
        parts.append("<p>" + tr(
            f"Ещё {finances.extra_channels} канал(ов). Откройте «Каналы».",
            f"{finances.extra_channels} additional channel(s). Open Channels.",
        ) + "</p>")
    return "\n".join(parts)
