"""Translate known owner service errors at the Telegram UI boundary.

Do not localize exceptions inside service code: these are also consumed by
state machines and tests. Unknown messages get a safe generic English error,
while Russian keeps the original diagnostic for existing operators.
"""
import re

from telegram_membership_saas.localization import tr

_ERROR_EN = {
    # Explicit, token-safe public errors from modules/bots/service.py.
    "Шифрование токенов ещё не настроено на сервере":
        "Bot token encryption is not configured. Contact platform support.",
    "Ключ шифрования ботов настроен некорректно":
        "Bot token encryption is misconfigured. Contact platform support.",
    "Подключение ботов доступно только владельцу проекта":
        "Only the project owner can connect bots.",
    "Неверный формат токена Telegram-бота":
        "Invalid Telegram bot token format. Get a token from @BotFather.",
    "Не удалось проверить токен Telegram-бота":
        "Could not verify your bot token. Request a new one from @BotFather.",
    "Telegram не подтвердил токен бота":
        "Telegram rejected the bot token. Check or regenerate it in @BotFather.",
    "Telegram не вернул корректную учётную запись бота":
        "Telegram did not return a valid bot identity.",
    "Нельзя подключить управляющего бота ViMi Pay как клиентского":
        "You cannot connect the ViMi Pay platform bot as a customer bot.",
    "Предыдущий бот ещё удаляется. Дождитесь завершения "
    "и подключите бота повторно.":
        "The previous bot is still being removed. Reconnect after removal completes.",
    "В этом проекте уже подключён другой бот. "
    "Пока поддерживается один бот на проект. "
    "Для другого бота создайте отдельный проект; "
    "токен текущего бота можно обновить здесь.":
        "Another bot is already connected to this project. "
        "Only one bot per project is supported; create a separate project "
        "or rotate the current bot's token here.",
    "Этот Telegram-бот уже привязан к другому проекту":
        "This Telegram bot is already linked to another project.",
    "Добавьте действующий способ оплаты для валюты тарифа":
        "Add an active payment method supporting the plan currency.",
    "Нет прав на управление этим ботом": "You cannot manage this bot.",
    "Сначала подключите бота": "Connect your bot first.",
    "Этот шаг уже заменён другим запросом":
        "A newer setup request replaced this step. Reopen the section.",
    "Шаг устарел. Откройте нужный раздел заново.":
        "This step has expired. Reopen the relevant section.",
    "Шаг настройки устарел":
        "This setup step has expired. Start again.",
    "Проект или шаг изменился. Начните действие заново.":
        "The project or setup step changed. Start this action again.",
    "Название проекта должно содержать от 2 до 120 символов":
        "Project name must contain 2 to 120 characters.",
    "Нельзя подключить управляющий бот ViMi Pay":
        "You cannot connect the ViMi Pay platform bot as a customer bot.",
    "Не удалось подтвердить владельца бота":
        "The bot owner could not be verified.",
    "Этот Telegram-бот уже подключён в другом проекте":
        "This Telegram bot is already connected to another project.",
    "Достигнут установленный лимит подключённых ботов. "
    "Удалите ненужное подключение или обратитесь в поддержку ViMi Pay.":
        "Your connected bot limit has been reached. Remove an unused "
        "connection or contact ViMi Pay support.",
    "Некорректное имя бота": "Invalid bot username.",
    "Сначала подключите одного собственного бота":
        "Connect your own Telegram bot first.",
    "Этот канал уже использовался в другом вашем подключении. "
    "Откройте прежнее подключение в @vimipay_bot → Мои подключения "
    "(при необходимости восстановите его из архива) и "
    "подключите нового бота там. Финансовая история сохранится.":
        "This channel belongs to an earlier connection of yours. "
        "Open @vimipay_bot → My connections, restore the old connection "
        "from the archive if needed, and connect your new bot there. "
        "The financial history remains intact.",
    "Подключённый бот требует повторной проверки токена":
        "The connected bot requires token verification again.",
    "Поддерживаются только каналы и супергруппы":
        "Only Telegram channels and supergroups are supported.",
    "Токен подключённого бота изменился":
        "The connected bot token changed. Reconnect the bot.",
    "Вы должны быть администратором выбранного канала":
        "You must be an administrator of the selected channel.",
    "Выдайте своему боту права добавления и блокировки пользователей":
        "Give your bot permission to invite and ban members.",
    "Не удалось проверить канал. Добавьте своего бота администратором "
    "и повторите выбор.":
        "Could not verify the channel. Add your bot as an administrator and retry.",
    "Название тарифа: от 2 до 120 символов":
        "Plan name must contain 2 to 120 characters.",
    "Неподдерживаемая длительность": "Unsupported plan duration.",
    "Укажите корректный срок тарифа": "Enter a valid plan duration.",
    "Неверный срок тарифа": "Invalid plan duration.",
    "Выберите действующий канал или группу":
        "Select an active channel or group.",
    "Инструкция слишком длинная (максимум 4000 символов)":
        "Payment instructions exceed the 4,000-character limit.",
    "Тариф не принадлежит выбранному проекту":
        "This plan does not belong to the selected project.",
    "Привяжите к тарифу действующий канал":
        "Assign an active channel to this plan.",
    "Сначала назначьте цену тарифа":
        "Set a price for this plan first.",
    "Добавьте действующий способ оплаты, поддерживающий валюту тарифа":
        "Add an active payment method that supports this plan currency.",
    "Недопустимая страница": "Invalid page. Refresh the list.",
    "Способ оплаты не найден в этом проекте":
        "Payment method not found in this project.",
    "Валюта не поддерживается": "Currency is not supported.",
    "Способ оплаты недоступен в этом проекте":
        "Payment method is not available in this project.",
    "Валюта временно недоступна": "Currency is temporarily unavailable.",
    "Способ оплаты не найден": "Payment method not found.",
    "Тариф не найден в этом проекте":
        "Plan not found in this project.",
    "Проверяйте заявки в собственном боте":
        "Review payment requests inside your connected bot.",
    "Срок можно изменить только у неопубликованного тарифа":
        "The duration can only be changed for an unpublished draft.",
    "Канал можно изменить только у неопубликованного тарифа":
        "The channel can only be changed for an unpublished draft.",
    "Снять с продажи можно только активный тариф":
        "Only an active plan can be removed from sale.",
    "Тариф изменился. Откройте карточку заново.":
        "This plan has changed. Open the plan details again.",
    "Тариф имеет историю заявок или подписок. Удаление запрещено — снимите его с продажи.":
        "This plan has payment or subscription history. Archive it instead of deleting.",
    "Тариф связан с покупками или доступом. Удаление запрещено.":
        "This plan has purchase or access history and cannot be deleted.",

}

# Public English strings already produced by tr(...) at handler boundaries.
# Keep only known, static UI strings: never leak arbitrary exception contents.
_KNOWN_EN_ERRORS = frozenset(_ERROR_EN.values()) | {
    "Bot connections are unavailable",
    "Create or select a connection first",
    "Another project menu. Reopen My bots.",
    "This bot cannot be removed.",
    "Project changed. Reopen My bots.",
    "Another project menu is open.",
    "Unknown action",
    "Another project menu. Refresh obligations.",
    "Action is stale. Refresh obligations.",
    "Action was completed or has expired.",
    "Bot connections are temporarily unavailable",
    "Unsupported chat type",
    "Connect an active channel or group first",
    "Unsupported duration",
    "Project changed. Please retry.",
    "Connect a channel first",
    "Unsupported currency",
    "This menu belongs to another project. Reopen settings.",
    "You cannot change another bot's settings",
    "Invalid token format. Get a token from @BotFather.",
    "Invalid token format. Get it from BotFather.",
    "Invalid duration",
    "Plan name must be 2–120 characters",
    "Payment method name must be 2–120 characters",
}

# Price-service errors are already public validation messages in English.
# Only these exact strings and bounded precision variants can reach the UI;
# never return an arbitrary exception or DB detail.
_PRICE_ERRORS_RU = {
    "Price amount must be a valid decimal number":
        "Введите корректную числовую сумму.",
    "Price amount must be finite and positive":
        "Цена должна быть положительным конечным числом.",
}


def _price_error(message: str) -> str | None:
    if message in _PRICE_ERRORS_RU:
        return tr(_PRICE_ERRORS_RU[message], message)
    match = re.fullmatch(
        r"Price amount supports at most ([0-9]{1,2}) (fractional|integer) digits",
        message,
    )
    if match is None:
        return None
    limit = int(match.group(1))
    kind = match.group(2)
    if kind == "fractional" and 0 <= limit <= 18:
        return tr(f"Цена: максимум {limit} знаков после запятой.", message)
    if kind == "integer" and limit == 20:
        return tr("Цена: максимум 20 цифр до запятой.", message)
    return None


def owner_error(message: str) -> str:
    """Preserve safe, already localized errors; redact unknown English exceptions."""
    price_message = _price_error(message)
    if price_message is not None:
        return price_message
    if message in _KNOWN_EN_ERRORS:
        return message
    return tr(message, _ERROR_EN.get(message, "Action unavailable. Reopen the section."))
