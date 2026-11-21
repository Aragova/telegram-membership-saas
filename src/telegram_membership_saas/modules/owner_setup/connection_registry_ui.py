"""Minimal bilingual Rich/classic connection registry with no unrelated finances.

The registry is a selector, not an account dashboard or a bot status report.
Counts reflect the owner-scoped list provided to the handler, including all pages.
"""
from __future__ import annotations

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


def _ru_count(value: int, *, archived: bool) -> str:
    one, few, many = (
        ("подключение", "подключения", "подключений")
        if archived
        else ("бот", "бота", "ботов")
    )
    ending = value % 100
    if 11 <= ending <= 14:
        noun = many
    elif value % 10 == 1:
        noun = one
    elif 2 <= value % 10 <= 4:
        noun = few
    else:
        noun = many
    return f"{value} {noun}"


def connection_registry_rich(
    *, total: int, page: int, total_pages: int, archived: bool = False,
) -> str:
    """Telegram Rich HTML, visually lightweight even for large bot networks."""
    if archived:
        headline = tr("📦 АРХИВ ПОДКЛЮЧЕНИЙ", "📦 ARCHIVED CONNECTIONS")
        count = tr(
            f"В архиве <b>{_ru_count(total, archived=True)}</b>.",
            f"<b>{total} archived connection{'s' if total != 1 else ''}</b>.",
        )
        next_step = tr(
            "Выберите подключение для просмотра или восстановления.",
            "Select a connection to view or restore.",
        )
        empty = tr(
            "📭 Архив пока пуст.", "📭 No archived connections yet.",
        )
    else:
        headline = tr("📂 МОИ ПОДКЛЮЧЕНИЯ", "📂 MY CONNECTIONS")
        connected = "подключён" if total % 10 == 1 and total % 100 != 11 else "подключено"
        count = tr(
            f"🤖 У вас {connected} <b>{_ru_count(total, archived=False)}</b>.",
            f"🤖 You have <b>{total} connected bot{'s' if total != 1 else ''}</b>.",
        )
        next_step = tr(
            "Выберите подключение для управления.",
            "Choose a connection to manage.",
        )
        empty = tr(
            "✨ У вас пока нет подключённых ботов. Добавьте первого ниже.",
            "✨ No connected bots yet. Add your first bot below.",
        )
    lines = ["<h3>" + headline + "</h3>"]
    if total:
        lines.append("<p>" + count + "<br><i>" + next_step + "</i></p>")
    else:
        lines.append("<p><i>" + empty + "</i></p>")
    if total_pages > 1:
        lines.append(
            "<p><i>📄 " + tr("Страница", "Page") + f" {page + 1}/{total_pages}"
            + "</i></p>"
        )
    lines.append(RICH_ACTIONS_MARKER)
    return "\n".join(lines)


def connection_registry_classic(
    *, total: int, page: int, total_pages: int, archived: bool = False,
) -> str:
    """Telegram-safe HTML fallback with no unsupported Rich tags."""
    if archived:
        heading = tr("📦 АРХИВ ПОДКЛЮЧЕНИЙ", "📦 ARCHIVED CONNECTIONS")
        count = tr(
            f"В архиве <b>{_ru_count(total, archived=True)}</b>.",
            f"<b>{total} archived connection{'s' if total != 1 else ''}</b>.",
        )
        next_step = tr(
            "Выберите подключение для просмотра или восстановления.",
            "Select a connection to view or restore.",
        )
        empty = tr("📭 Архив пока пуст.", "📭 No archived connections yet.")
    else:
        heading = tr("📂 МОИ ПОДКЛЮЧЕНИЯ", "📂 MY CONNECTIONS")
        connected = "подключён" if total % 10 == 1 and total % 100 != 11 else "подключено"
        count = tr(
            f"🤖 У вас {connected} <b>{_ru_count(total, archived=False)}</b>.",
            f"🤖 You have <b>{total} connected bot{'s' if total != 1 else ''}</b>.",
        )
        next_step = tr(
            "Выберите подключение для управления.",
            "Choose a connection to manage.",
        )
        empty = tr(
            "✨ У вас пока нет подключённых ботов. Добавьте первого ниже.",
            "✨ No connected bots yet. Add your first bot below.",
        )
    lines = ["<b>" + heading + "</b>"]
    if total:
        lines.extend([count, "<i>" + next_step + "</i>"])
    else:
        lines.append("<i>" + empty + "</i>")
    if total_pages > 1:
        lines.extend(["", "📄 " + tr("Страница", "Page")
                      + f" {page + 1}/{total_pages}"])
    return "\n".join(lines)
