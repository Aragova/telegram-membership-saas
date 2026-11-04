"""Translate known owner service errors at the Telegram UI boundary.

Do not localize exceptions inside service code: these are also consumed by
state machines and tests. Unknown messages get a safe generic English error,
while Russian keeps the original diagnostic for existing operators.
"""
from telegram_membership_saas.localization import tr

_ERROR_EN = {
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
    "Некорректное имя бота": "Invalid bot username.",
    "Сначала подключите одного собственного бота":
        "Connect your own Telegram bot first.",
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
}


def owner_error(message: str) -> str:
    """A bounded public message; never leak unexpected internals in English."""
    return tr(message, _ERROR_EN.get(message, "Action unavailable. Reopen the section."))
