"""Compact RU/EN Rich screens for Stars onboarding and recipient selection.

Financial implementation details belong to operator tools, not merchant cards.
Legacy paid orders and existing plan actions are unchanged by these renderers.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr


def overview_rich(*, plans: list[tuple[str, str, str]]) -> str:
    lines = [
        "<h3>⭐ Telegram Stars</h3>",
        "<p>" + tr(
            "Принимайте оплату подписок в звёздах Telegram.",
            "Accept subscription payments in Telegram Stars.",
        ) + "</p>",
        "<h4>" + tr("📦 ВАШИ ТАРИФЫ", "📦 YOUR PLANS") + "</h4>",
    ]
    if not plans:
        lines.extend([
            "<p>" + tr("⚪ Пока нет созданных тарифов.",
                       "⚪ No plans yet.") + "</p>",
            "<p>" + tr(
                "Создайте первый тариф и выберите, <b>куда будут "
                "поступать звёзды</b>.",
                "Create your first plan and choose <b>where Stars "
                "will be credited</b>.",
            ) + "</p>",
        ])
    else:
        lines.append("<table bordered compact>")
        for name, price, status in plans:
            lines.append(
                "<tr><td><b>" + escape(name) + "</b></td><td>"
                + escape(price) + "</td><td>" + escape(status) + "</td></tr>"
            )
        lines.append("</table>")
    return "\n".join(lines)


def overview_classic(*, plans: list[tuple[str, str, str]]) -> str:
    lines = [
        "⭐ <b>Telegram Stars</b>",
        tr("Принимайте оплату подписок в звёздах Telegram.",
           "Accept subscription payments in Telegram Stars."),
        "<b>" + tr("📦 ВАШИ ТАРИФЫ", "📦 YOUR PLANS") + "</b>",
    ]
    if plans:
        lines.extend(
            "• <b>" + escape(name) + "</b> · " + escape(price)
            + " · " + escape(status)
            for name, price, status in plans
        )
    else:
        lines.extend([
            tr("⚪ Пока нет созданных тарифов.", "⚪ No plans yet."),
            tr("Создайте первый тариф и выберите, <b>куда будут "
               "поступать звёзды</b>.",
               "Create your first plan and choose <b>where Stars "
               "will be credited</b>."),
        ])
    return "\n\n".join(lines)


def recipient_rich() -> str:
    return "\n".join([
        "<h3>" + tr(
            "⭐ Куда получать звёзды?", "⭐ Where should Stars go?",
        ) + "</h3>",
        "<p>" + tr(
            "Выберите способ получения оплаты за подписки.",
            "Choose how to receive subscription payments.",
        ) + "</p>",
        "<h4>" + tr("🤖 НА БАЛАНС БОТА", "🤖 TO BOT BALANCE") + "</h4>",
        "<p>🟢 <b>" + tr(
            "ViMi Pay управляет подписками.",
            "ViMi Pay manages subscriptions.",
        ) + "</b></p>",
        "<p>" + tr(
            "Покупатель платит боту. ViMi Pay подтверждает покупку, "
            "выдаёт доступ и учитывает продажи.",
            "Buyers pay the bot. ViMi Pay confirms purchases, "
            "grants access and records sales.",
        ) + "</p>",
        "<h4>" + tr("📢 НА БАЛАНС КАНАЛА", "📢 TO CHANNEL BALANCE") + "</h4>",
        "<p>🔵 <b>" + tr(
            "Telegram управляет подписками.",
            "Telegram manages subscriptions.",
        ) + "</b></p>",
        "<p>" + tr(
            "Звёзды получает канал. Telegram выдаёт доступ "
            "и продлевает подписку каждые 30 дней.",
            "Stars go to the channel. Telegram grants access "
            "and renews subscriptions every 30 days.",
        ) + "</p>",
    ])


def recipient_classic() -> str:
    return "\n\n".join([
        "⭐ <b>" + tr(
            "Куда получать звёзды?", "Where should Stars go?",
        ) + "</b>",
        tr("Выберите способ получения оплаты за подписки.",
           "Choose how to receive subscription payments."),
        "<b>" + tr("🤖 НА БАЛАНС БОТА", "🤖 TO BOT BALANCE")
        + "</b>\n🟢 " + tr(
            "ViMi Pay подтверждает покупки, выдаёт доступ и учитывает продажи.",
            "ViMi Pay verifies purchases, grants access and records sales.",
        ),
        "<b>" + tr("📢 НА БАЛАНС КАНАЛА", "📢 TO CHANNEL BALANCE")
        + "</b>\n🔵 " + tr(
            "Telegram выдаёт доступ и продлевает подписку каждые 30 дней. "
            "Звёзды получает канал.",
            "Telegram grants access and renews every 30 days. "
            "Stars go to the channel.",
        ),
    ])


def channel_picker_rich(*, mode: str) -> str:
    return "\n".join([
        "<h3>" + tr(
            "📢 Выберите канал или группу" if mode == "bot"
            else "📢 Выберите приватный канал",
            "📢 Choose a channel or group" if mode == "bot"
            else "📢 Choose a private channel",
        ) + "</h3>",
        "<p>" + tr(
            "Где будет доступна подписка?",
            "Where should the subscription grant access?",
        ) + "</p>",
        *(
            ["<p>" + tr(
                "Для получения ⭐ на канал поддерживается только "
                "<b>приватный канал</b>, не группа.",
                "Channel Stars require a <b>private channel</b>, "
                "not a group.",
            ) + "</p>"]
            if mode == "channel" else []
        ),
    ])


def channel_picker_classic(*, mode: str) -> str:
    return "\n\n".join([
        tr(
            "📢 <b>Выберите канал или группу</b>" if mode == "bot"
            else "📢 <b>Выберите приватный канал</b>",
            "📢 <b>Choose a channel or group</b>" if mode == "bot"
            else "📢 <b>Choose a private channel</b>",
        ),
        tr("Где будет доступна подписка?",
           "Where should the subscription grant access?"),
        *(
            [tr("⭐ На баланс канала доступны только приватные каналы.",
                "Only private channels can receive native channel Stars.")]
            if mode == "channel" else []
        ),
    ])


def price_rich(*, mode: str, warning: bool = False) -> str:
    channel = mode == "channel"
    top = 10000 if channel else 250000
    head = tr(
        "⭐ Подписка на канал" if channel else "⭐ Создание тарифа",
        "⭐ Channel subscription" if channel else "⭐ Create a plan",
    )
    body = [
        "<h3>" + head + "</h3>",
        "<table compact>",
        "<tr><td>📅 " + tr("Срок", "Period") + "</td><td><b>"
        + (tr("30 дней", "30 days") if channel else tr("1 месяц", "1 month"))
        + "</b></td></tr>",
        "<tr><td>" + (
            tr("📢 Получатель", "📢 Recipient")
            if channel else tr("🤖 Получатель", "🤖 Recipient")
        ) + "</td><td><b>" + (
            tr("Баланс канала", "Channel balance")
            if channel else tr("Баланс бота", "Bot balance")
        ) + "</b></td></tr>",
        "</table>",
    ]
    if warning:
        body.append("<p>⚠️ <b>" + tr(
            "Неверная цена. Отправьте целое число в указанном диапазоне.",
            "Invalid price. Send a whole number in the stated range.",
        ) + "</b></p>")
    body.extend([
        "<h4>" + tr("💰 УКАЖИТЕ ЦЕНУ", "💰 ENTER YOUR PRICE") + "</h4>",
        "<p>➡️ " + tr(
            "Отправьте <b>число звёзд прямо в этот чат</b>.",
            "Send the <b>Stars amount right here in this chat</b>.",
        ) + "</p>",
        "<p>" + tr(
            f"От 1 до {top:,} ⭐. Например: <b>150</b>.",
            f"From 1 to {top:,} ⭐. Example: <b>150</b>.",
        ).replace(",", " ") + "</p>",
        "<p>" + (
            tr(
                "После ввода цены подтвердите создание платной ссылки.",
                "After entering the price, confirm the paid invite link.",
            ) if channel else tr(
                "После сохранения тариф появится в продаже.",
                "The plan will be published after saving.",
            )
        ) + "</p>",
    ])
    return "\n".join(body)


def price_classic(*, mode: str, warning: bool = False) -> str:
    channel = mode == "channel"
    max_price = "10 000" if channel else "250 000"
    parts = [
        "⭐ <b>" + tr(
            "Подписка на канал" if channel else "Создание тарифа",
            "Channel subscription" if channel else "Create a plan",
        ) + "</b>",
        "📅 " + tr("Срок: ", "Period: ") + "<b>"
        + (tr("30 дней", "30 days") if channel else tr("1 месяц", "1 month"))
        + "</b>\n" + tr(
            "📢 Получатель: <b>Баланс канала</b>",
            "📢 Recipient: <b>Channel balance</b>",
        ) if channel else
        "📅 " + tr("Срок: ", "Period: ") + "<b>"
        + tr("1 месяц", "1 month") + "</b>\n"
        + tr("🤖 Получатель: <b>Баланс бота</b>",
             "🤖 Recipient: <b>Bot balance</b>"),
    ]
    if warning:
        parts.append("⚠️ " + tr(
            "Отправьте целое число в указанном диапазоне.",
            "Send a whole number in the stated range.",
        ))
    parts.extend([
        "<b>" + tr("💰 УКАЖИТЕ ЦЕНУ", "💰 ENTER YOUR PRICE") + "</b>",
        "➡️ " + tr(
            "Отправьте <b>число звёзд прямо сюда</b>.",
            "Send the <b>Stars amount right here</b>.",
        ) + "\n" + tr(
            f"От 1 до {max_price} ⭐. Например: 150.",
            f"From 1 to {max_price} ⭐. Example: 150.",
        ),
        tr(
            "После ввода цены подтвердите создание платной ссылки.",
            "After entering the price, confirm the paid invite link.",
        ) if channel else tr(
            "После сохранения тариф появится в продаже.",
            "The plan will be published after saving.",
        ),
    ])
    return "\n\n".join(parts)


def native_confirm_rich(*, stars: int) -> str:
    return "\n".join([
        "<h3>" + tr(
            "📢 Создать платную ссылку?", "📢 Create a paid invite?",
        ) + "</h3>",
        "<p>" + tr(
            "⭐ Звёзды будут поступать <b>на баланс канала</b>.",
            "⭐ Stars will go <b>to the channel balance</b>.",
        ) + "</p>",
        "<table compact>",
        "<tr><td>📅 " + tr("Подписка", "Subscription")
        + "</td><td><b>" + tr("30 дней", "30 days") + "</b></td></tr>",
        "<tr><td>💰 " + tr("Цена", "Price") + "</td><td><b>"
        + str(stars) + " ⭐</b></td></tr>",
        "</table>",
        "<p>" + tr(
            "Telegram будет управлять доступом и продлениями.",
            "Telegram will manage access and renewals.",
        ) + "</p>",
        "<p>" + tr(
            "До подтверждения тариф не публикуется.",
            "The plan will not be published before confirmation.",
        ) + "</p>",
    ])


def native_confirm_classic(*, stars: int) -> str:
    return "\n\n".join([
        "📢 <b>" + tr(
            "Создать платную ссылку?", "Create a paid invite?",
        ) + "</b>",
        tr("⭐ На баланс канала · 30 дней · ",
           "⭐ To channel balance · 30 days · ")
        + f"<b>{stars} ⭐</b>",
        tr("Доступом и продлениями управляет Telegram. "
           "До подтверждения тариф не публикуется.",
           "Telegram manages access and renewals. "
           "Nothing is published before confirmation."),
    ])


def success_rich(*, mode: str, stars: int) -> str:
    channel = mode == "channel"
    recipient = tr(
        "📢 Звёзды поступают на баланс канала. "
        "Доступ и продления обслуживает Telegram.",
        "📢 Stars go to the channel balance. "
        "Telegram manages access and renewals.",
    ) if channel else tr(
        "🤖 Звёзды поступают на баланс бота. "
        "ViMi Pay управляет доступом.",
        "🤖 Stars go to the bot balance. "
        "ViMi Pay manages access.",
    )
    return "\n".join([
        "<h3>" + tr(
            "✅ Платная ссылка создана!" if channel
            else "✅ Тариф опубликован!",
            "✅ Paid invite created!" if channel
            else "✅ Plan published!",
        ) + "</h3>",
        "<p>⭐ " + tr("Цена: ", "Price: ")
        + "<b>" + str(stars) + " ⭐</b></p>",
        "<p>" + recipient + "</p>",
    ])


def success_classic(*, mode: str, stars: int) -> str:
    return "\n\n".join([
        "✅ <b>" + tr(
            "Платная ссылка создана!" if mode == "channel"
            else "Тариф опубликован!",
            "Paid invite created!" if mode == "channel"
            else "Plan published!",
        ) + "</b>",
        "⭐ " + tr("Цена: ", "Price: ") + f"<b>{stars} ⭐</b>",
        tr(
            "📢 Баланс канала · Telegram управляет доступом.",
            "📢 Channel balance · Telegram manages access.",
        ) if mode == "channel" else tr(
            "🤖 Баланс бота · ViMi Pay управляет доступом.",
            "🤖 Bot balance · ViMi Pay manages access.",
        ),
    ])
