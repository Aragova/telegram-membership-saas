"""Private ViMi Pay operator menu. No tenant settings or payment mutation.

One Telegram account can navigate between platform and personal bot registry,
but each button performs a fresh server-side role check.
"""
from __future__ import annotations

from html import escape

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
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_dashboard import (
    PAGE_SIZE,
    PERIOD_LABELS,
    PERIODS,
    fmt_turnover,
    is_configured_platform_owner,
    platform_report,
)


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
            ("✓ " if period == current else "") + PERIOD_LABELS[period],
            f"{section}:{period}:0",
        ) for period in PERIODS
    ]
    return [result[:3], result[3:]]


def _summary_text(snapshot) -> str:
    stats = snapshot.counts
    text = [
        "✦ <b>ViMi Pay · Платформа</b>",
        "Статистика всей сети",
        "",
        f"🤖 Подключённых ботов: <b>{stats['bots']}</b>",
        f"✅ Зарегистрировано: {stats['bots_active']}",
        f"🟢 Работают сейчас: {stats['bots_running']}",
        f"▫️ Отключено: {stats['bots_disabled']}",
        f"👥 Владельцев: <b>{stats['owners']}</b>",
        f"📢 Каналов и групп: {stats['resources']}",
        f"💎 Опубликованных тарифов: {stats['plans_published']}",
        f"📝 Черновиков тарифов: {stats['plans_draft']}",
        f"🔐 Активных подписок: {stats['subscriptions_active']}",
        f"📋 Заявок на проверке: {stats['requests_pending']}",
        "",
        f"<b>{PERIOD_LABELS[snapshot.period]}</b>",
        f"Новых ботов: {stats['bots_new_period']}",
        f"Новых подписок: {stats['subscriptions_new_period']}",
        f"Подтверждённых оплат: {stats['payments_period']}",
        "",
        "💰 <b>Оборот клиентов по валютам</b>",
    ]
    if snapshot.turnover:
        text.extend(
            "• " + escape(str(item["currency"])) + " — "
            + escape(fmt_turnover(str(item["amount"])))
            + " (операций: " + str(item["count"]) + ")"
            for item in snapshot.turnover
        )
    else:
        text.append("Подтверждённых платежей за период нет.")
    text.extend([
        "",
        "<i>Суммы подтверждены администраторами каналов, "
        "но не проверены ViMi Pay в банках.</i>",
        "Выручка ViMi Pay: <b>биллинг ещё не запущен</b>.",
    ])
    return "\n".join(text)


def _bot_list(snapshot) -> str:
    lines = [
        "✦ <b>Подключённые боты</b>",
        "Период: " + PERIOD_LABELS[snapshot.period],
        "",
    ]
    if not snapshot.bots:
        lines.append("На этой странице нет подключений.")
    for idx, bot in enumerate(snapshot.bots, 1):
        lines.extend([
            f"<b>{idx + snapshot.page * PAGE_SIZE}. "
            + escape("@" + bot["username"]) + "</b>",
            "Работа: " + escape(bot["runtime_state"]),
            "Тарифов: " + str(bot["plans"])
            + " · Подписок: " + str(bot["subscriptions"])
            + " · Оплат: " + str(bot["payments_period"]),
            "",
        ])
    lines.append(
        "Страница " + str(snapshot.page + 1) + " из "
        + str(max(1, snapshot.pages))
    )
    return "\n".join(lines)


def _bot_details(bot: dict, *, period: str) -> str:
    currency_rows = bot.get("gmv_by_currency") or []
    finance = (
        "\n".join(
            "• " + escape(item["currency"]) + " — "
            + escape(fmt_turnover(str(item["amount"])))
            for item in currency_rows
        ) if currency_rows else "Подтверждённых оплат за период нет."
    )
    return (
        "✦ <b>Статистика бота</b>\n\n"
        "🤖 <b>@" + escape(bot["username"]) + "</b>\n"
        "Проект: " + escape(bot["project_name"]) + "\n"
        "Подключение: " + escape(bot["status"]) + "\n"
        "Работа: " + escape(bot["runtime_state"]) + "\n\n"
        "📢 Ресурсов: " + str(bot["resources"]) + "\n"
        "💎 Опубликованных тарифов: " + str(bot["plans"]) + "\n"
        "🔐 Активных подписок: " + str(bot["subscriptions"]) + "\n"
        "💳 Подтверждённых оплат ("
        + escape(PERIOD_LABELS[period]) + "): "
        + str(bot["payments_period"]) + "\n\n"
        "💰 <b>Оборот по валютам</b>\n"
        + finance + "\n\n"
        "<i>Только агрегаты. Управление каналом и платежами "
        "осуществляется в собственном боте владельца.</i>"
    )


def create_platform_dashboard_router(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings | None = None,
) -> Router:
    config = settings or get_settings()
    router = Router(name="platform-dashboard")

    async def read_report(user, *, period: str, page: int = 0):
        if not is_configured_platform_owner(user.id, config):
            raise PermissionError("Нет доступа к платформе")
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
            [_button("🤖 Все подключённые боты", f"bots:{period}:0")],
            [InlineKeyboardButton(
                text="🤖 Мои боты · личный режим",
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
            await send_platform_home(bot, message.from_user, period="30")
        except Exception:
            # No internals, credentials, or SQL errors in operator chat.
            await bot.send_message(
                message.from_user.id,
                "👑 Панель платформы пока недоступна. "
                "Проверьте подключение и права оператора.",
            )

    @router.callback_query(F.data.startswith("plat:"))
    async def platform_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
            or not is_configured_platform_owner(query.from_user.id, config)
        ):
            await query.answer("Нет доступа к панели платформы", show_alert=True)
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
                        f"detail:{period}:{page}:{idx}",
                    )])
                nav = []
                if page > 0:
                    nav.append(_button("⬅️", f"bots:{period}:{page - 1}"))
                if page + 1 < report.pages:
                    nav.append(_button("➡️", f"bots:{period}:{page + 1}"))
                if nav:
                    rows.append(nav)
                rows.append([_button("⬅️ Платформа", f"home:{period}:0")])
                await bot.send_message(
                    query.from_user.id, _bot_list(report),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif len(tokens) == 5 and tokens[1] == "detail":
                _, _, period, page_token, index_token = tokens
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
                if index >= len(report.bots):
                    raise ValueError("Карточка устарела")
                await bot.send_message(
                    query.from_user.id,
                    _bot_details(report.bots[index], period=period),
                    parse_mode="HTML", reply_markup=_keyboard([
                        [_button("⬅️ Все боты", f"bots:{period}:{page}")],
                    ]),
                )
            else:
                raise ValueError("Кнопка устарела")
        except (PermissionError, ValueError):
            await query.answer("Недоступно. Откройте /platform заново.", show_alert=True)
            return
        except Exception:
            await query.answer("Отчёт временно недоступен.", show_alert=True)
            return
        await query.answer()

    return router
