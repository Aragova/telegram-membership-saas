"""Translate fixed owner/central navigation without touching user-entered names."""
from telegram_membership_saas.localization import tr

BUTTON_LABELS_EN = {
    "➕ Подключить бота подписок": "➕ Connect subscription bot",
    "🤖 Управление ботом": "🤖 Manage bot",
    "📁 Все подключения": "📁 All connections",
    "❔ Как это работает": "❔ How it works",
    "📢 Каналы": "📢 Channels",
    "💳 Оплата": "💳 Payments",
    "📦 Тарифы": "📦 Plans",
    "📋 Заявки": "📋 Payment reviews",
    "📋 Готовность": "📋 Readiness",
    "👀 Смотреть гостем": "👀 Buyer preview",
    "❔ Помощь": "❔ Help",
    "🔄 Обновить статус": "🔄 Refresh status",
    "➕ Подключить / обновить токен": "➕ Connect / change token",
    "➕ Подключить другого бота": "➕ Connect another bot",
    "⬅️ Мои подключения": "⬅️ My connections",
    "⬅️ Назад": "⬅️ Back",
    "➡️ Далее": "Next ➡️",
    "🔄 Обновить обязательства": "🔄 Refresh obligations",
    "⬅️ Мои боты": "⬅️ My bots",
    "➕ Выбрать канал": "➕ Select channel",
    "➕ Выбрать группу": "➕ Select group",
    "✍️ Ввести ID / @username": "✍️ Enter ID / @username",
    "➡️ Далее: способы оплаты": "Next: payment methods ➡️",
    "⬅️ Панель администратора": "⬅️ Admin panel",
    "➕ Создать тариф": "➕ Create plan",
    "➕ Настроить оплату": "➕ Configure payments",
    "➕ Подключить способ оплаты": "➕ Add payment method",
    "✅ Опубликовать тариф": "✅ Publish plan",
    "⬅️ Все тарифы": "⬅️ All plans",
    "➕ Добавить способ": "➕ Add method",
    "➡️ Перейти к тарифам": "Go to plans ➡️",
    "🌐 Добавить валюту": "🌐 Add currency",
    "⏸️ Отключить для новых покупок": "⏸️ Disable for new purchases",
    "▶️ Включить способ оплаты": "▶️ Enable payment method",
    "⬅️ Все способы оплаты": "⬅️ All payment methods",
    "🔄 Обновить заявки": "🔄 Refresh requests",
    "⬅️ Панель управления": "⬅️ Dashboard",
    "➕ Подключить бота": "➕ Connect bot",
    "🗑 Да, удалить бота": "🗑 Yes, remove bot",
    "🧾 Управлять обязательствами": "🧾 Manage obligations",
    "Отмена": "Cancel",
    "✅ Подтверждаю действие": "✅ Confirm action",
    "➕ Настроить способ оплаты": "➕ Configure payment method",
    "⬅️ Вернуться к тарифу": "⬅️ Back to plan",
    "⬅️ Вернуться": "⬅️ Return",
    "⏸️ Подтвердить отключение": "⏸️ Confirm disabling",
    "✍️ Указать другой": "✍️ Choose another",
}


def owner_button_label(value: str) -> str:
    return tr(value, BUTTON_LABELS_EN.get(value, value))


RUNTIME_LABELS_EN = {
    "⚪ Ожидает запуска": "⚪ Waiting to start",
    "⚪ Нет связи с ботом": "⚪ Bot is offline",
    "🟢 Работает": "🟢 Running",
    "🟡 Запускается": "🟡 Starting",
    "🟡 В очереди на запуск": "🟡 Queued to start",
    "🟡 Требуется настройка": "🟡 Setup required",
    "🔴 Ошибка токена — подключите бота повторно": "🔴 Invalid token — reconnect the bot",
    "🔴 Проверьте токен и webhook бота": "🔴 Check the bot token and webhook",
    "🔴 Проверьте права бота в канале": "🔴 Check bot permissions in the channel",
    "🟡 Бот перезапускается": "🟡 Bot is restarting",
    "🔴 Не удалось запустить бота": "🔴 Unable to start bot",
    "⚪ Не запущен": "⚪ Not running",
    "⚪ Не подключён": "⚪ Not connected",
    "🟡 Бот работает, но тарифы или оплата не настроены":
        "🟡 Bot runs, but plans or payments are not set up",
}


def owner_runtime_label(value: str) -> str:
    return tr(value, RUNTIME_LABELS_EN.get(value, value))
