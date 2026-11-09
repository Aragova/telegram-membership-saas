"""Single official ViMi Pay buyer-support destination across connected bots."""
from aiogram.types import InlineKeyboardButton

from telegram_membership_saas.localization import tr

SUPPORT_URL = "https://t.me/vimi_support_bot"


def support_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=tr("🆘 Поддержка", "🆘 Support"),
        url=SUPPORT_URL, style="success",
    )
