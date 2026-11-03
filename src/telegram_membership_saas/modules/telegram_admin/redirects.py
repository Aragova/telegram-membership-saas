"""Safe central-bot handling of old payment controls after buyer cutover."""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message


def create_central_cutover_router() -> Router:
    router = Router(name="central-buyer-redirect")

    @router.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
    async def old_payment_decision_handler(callback: CallbackQuery) -> None:
        # These buttons belong to old central messages. Never approve with
        # the central Telegram identity after access has switched to customer bot.
        await callback.answer(
            "Откройте своего бота и отправьте /payments для работы с заявками.",
            show_alert=True,
        )

    @router.message(Command("payments", "plans"))
    async def old_customer_command_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await bot.send_message(
            chat_id=message.from_user.id,
            text=(
                "Управление оплатами и покупками перенесено в вашего "
                "подключённого бота. Откройте его и используйте /payments "
                "для заявок или /start для тарифов."
            ),
        )

    return router
