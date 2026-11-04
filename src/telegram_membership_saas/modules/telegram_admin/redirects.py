"""Safe central-bot handling of old payment controls after buyer cutover."""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from telegram_membership_saas.localization import tr


def create_central_cutover_router() -> Router:
    router = Router(name="central-buyer-redirect")

    @router.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
    async def old_payment_decision_handler(callback: CallbackQuery) -> None:
        # These buttons belong to old central messages. Never approve with
        # the central Telegram identity after access has switched to customer bot.
        await callback.answer(
            tr("Откройте своего бота и отправьте /payments для работы с заявками.",
               "Open your connected bot and send /payments to review requests."),
            show_alert=True,
        )

    @router.message(Command("payments", "plans"))
    async def old_customer_command_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await bot.send_message(
            chat_id=message.from_user.id,
            text=tr(
                "Управление оплатами и покупками перенесено в вашего "
                "подключённого бота. Откройте его и используйте /payments "
                "для заявок или /start для тарифов.",
                "Purchases and payment reviews have moved to your "
                "connected bot. Open it and use /payments to review requests "
                "or /start to view plans.",
            ),
        )

    return router
