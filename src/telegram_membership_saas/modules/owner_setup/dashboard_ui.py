"""Compact RU/EN dashboard: two 2-cell rows and verified Telegram finances.

Render-only: readiness comes from setup state, owner from Telegram's creator,
and the bot's Stars balance from the official Bot API. No invented channel
balance, available withdrawal, or beneficiary identity.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _finance_rows(
    channel: ChannelFinance, *, timezone: str,
) -> tuple[tuple[str, str], ...]:
    """Render only verified, fresh official Telegram revenue snapshots."""
    revenue = channel.revenue
    if revenue is None:
        return (
            (tr("📢 Баланс канала", "📢 Channel balance"),
             tr("Недоступен", "Unavailable")),
            (tr("💸 Вывод", "💸 Withdrawal"),
             tr("⚪ Не проверен", "⚪ Not verified")),
        )
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    rows = [
        (tr("📢 Баланс канала", "📢 Channel balance"),
         f"{revenue.current_balance:f} ⭐"),
        (tr("💰 Доступно к выводу", "💰 Withdrawable"),
         f"{revenue.available_balance:f} ⭐"),
    ]
    unlock = revenue.next_withdrawal_at
    waiting = unlock is not None and unlock > datetime.now(UTC)
    if waiting:
        outcome = tr("⏳ Ожидание", "⏳ Waiting")
    elif not revenue.withdrawal_enabled:
        outcome = tr("⛔ Недоступен", "⛔ Unavailable")
    elif revenue.available_balance == 0:
        outcome = tr("⚪ Нет доступных звёзд", "⚪ No withdrawable Stars")
    else:
        outcome = tr("✅ Разрешён Telegram", "✅ Allowed by Telegram")
    rows.append((tr("💸 Вывод", "💸 Withdrawal"), outcome))
    if waiting:
        rows.append((
            tr("⏳ Ожидание до", "⏳ Waiting until"),
            unlock.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        ))
    rows.append((
        tr("🕒 Проверено", "🕒 Checked"),
        revenue.observed_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
    ))
    return tuple(rows)


def classic_dashboard(
    *, project_name: str, timezone: str, trial_banner: str,
    readiness: tuple[tuple[str, bool], ...], sales_status: str,
    finances: DashboardFinances, exempt: bool = False,
) -> str:
    statuses = "\n".join(
        "  ·  ".join(
            _status_text(label, ready) + EMOJI_GAP
            + ("🟢" if ready else "⚪") + EMOJI_GAP
            + ("ON" if ready else "OFF")
            for label, ready in group
        )
        for group in (readiness[:2], readiness[2:4])
    )
    items = [
        tr("✦ <b>Панель администратора</b>", "✦ <b>Admin dashboard</b>")
        + (" · 👑 ViMi Pay PRO" if exempt else ""),
        "<b>" + escape(project_name) + "</b>"
        + EMOJI_GAP + "🌍" + EMOJI_GAP + escape(timezone),
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
    if not finances.available:
        items.append(tr("📢 Данные каналов временно недоступны",
                        "📢 Channel data temporarily unavailable"))
    for channel in finances.channels:
        if len(finances.channels) > 1 or finances.extra_channels:
            items.append("📢 <b>" + escape(channel.name) + "</b>")
        items.extend(
            escape(label) + ": " + escape(value)
            for label, value in _finance_rows(channel, timezone=timezone)
        )
        items.extend(
            escape(label) + ": " + value
            for label, value in _channel_rows(channel)
        )
    if not finances.channels and finances.available:
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
    """Use native Rich HTML tables without custom CSS or unsupported badges."""
    parts = [
        "<h3>" + tr("✦ Панель администратора", "✦ Admin dashboard")
        + (" · 👑 ViMi Pay PRO" if exempt else "") + "</h3>",
        "<p><b>" + escape(project_name) + "</b>" + EMOJI_GAP
        + "🌍" + EMOJI_GAP + escape(timezone) + "</p>",
    ]
    if trial_banner:
        parts.append("<p><b>" + trial_banner.replace("\n", "<br>") + "</b></p>")
    # Exactly two rows of two actionable readiness checks, RU and EN alike.
    for group in (readiness[:2], readiness[2:4]):
        parts.append("<table bordered compact><tr>")
        parts.extend(_status_cell(label, ready) for label, ready in group)
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
    if not finances.available:
        parts.append("<p>" + tr(
            "📢 Данные каналов временно недоступны",
            "📢 Channel data temporarily unavailable",
        ) + "</p>")
    if not finances.channels and finances.available:
        parts.append("<p>" + tr(
            "📢 Подключённых каналов нет",
            "📢 No connected channels",
        ) + "</p>")
    for channel in finances.channels:
        if len(finances.channels) > 1 or finances.extra_channels:
            parts.append("<p><b>📢" + EMOJI_GAP + escape(channel.name)
                         + "</b></p>")
        parts.append("<table bordered compact>")
        for label, value in _finance_rows(channel, timezone=timezone):
            parts.append(
                "<tr><th>" + escape(label) + "</th><td>"
                + escape(value) + "</td></tr>"
            )
        for label, value in _channel_rows(channel):
            parts.append(
                "<tr><th>" + escape(label) + "</th><td>"
                + value + "</td></tr>"
            )
        parts.append("</table>")
    if finances.extra_channels:
        parts.append("<p>" + tr(
            f"Ещё {finances.extra_channels} канал(ов). Откройте «Каналы».",
            f"{finances.extra_channels} additional channel(s). Open Channels.",
        ) + "</p>")
    return "\n".join(parts)
