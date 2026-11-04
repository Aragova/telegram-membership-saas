from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import recipient_language, tr

from telegram_membership_saas.modules.access import AccessActionType, AccessCommand

ACTIVE_MEMBER_STATUSES = frozenset({"creator", "administrator", "member"})


@dataclass(frozen=True, slots=True)
class TelegramAccessCommand:
    access: AccessCommand
    telegram_user_id: int
    chat_id: int | str


@dataclass(frozen=True, slots=True)
class TelegramAccessResult:
    invite_link: str | None = None
    invite_expires_at: datetime | None = None
    resolved_chat_id: int | None = None
    already_member: bool = False


class TelegramAccessAdapter:
    def __init__(
        self,
        bot: Bot,
        *,
        invite_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        if invite_ttl.total_seconds() <= 0:
            raise ValueError("invite_ttl must be positive")
        self._bot = bot
        self._invite_ttl = invite_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._session_factory = session_factory

    @property
    def invite_ttl(self) -> timedelta:
        return self._invite_ttl

    async def prepare(self, command: TelegramAccessCommand) -> TelegramAccessResult:
        if command.access.action_type == AccessActionType.GRANT:
            return await self._prepare_grant(command)
        if command.access.action_type == AccessActionType.REVOKE:
            await self._bot.ban_chat_member(
                chat_id=command.chat_id,
                user_id=command.telegram_user_id,
                revoke_messages=False,
            )
            return TelegramAccessResult()
        raise ValueError(f"Unsupported access action: {command.access.action_type}")

    async def _prepare_grant(self, command: TelegramAccessCommand) -> TelegramAccessResult:
        chat = await self._bot.get_chat(chat_id=command.chat_id)
        resolved_chat_id = int(chat.id)

        member = await self._bot.get_chat_member(
            chat_id=resolved_chat_id,
            user_id=command.telegram_user_id,
        )
        status = getattr(member.status, "value", member.status)
        is_current_member = status in ACTIVE_MEMBER_STATUSES or (
            status == "restricted" and bool(getattr(member, "is_member", False))
        )
        if is_current_member:
            return TelegramAccessResult(
                resolved_chat_id=resolved_chat_id,
                already_member=True,
            )

        await self._bot.unban_chat_member(
            chat_id=resolved_chat_id,
            user_id=command.telegram_user_id,
            only_if_banned=True,
        )

        expires_at = self._clock() + self._invite_ttl
        invite = await self._bot.create_chat_invite_link(
            chat_id=resolved_chat_id,
            name=command.access.project_id.hex,
            expire_date=expires_at,
            creates_join_request=True,
        )
        return TelegramAccessResult(
            invite_link=invite.invite_link,
            invite_expires_at=expires_at,
            resolved_chat_id=resolved_chat_id,
        )

    async def deliver(
        self,
        command: TelegramAccessCommand,
        result: TelegramAccessResult,
    ) -> None:
        if result.invite_link is None:
            return
        async with recipient_language(self._session_factory, command.telegram_user_id):
            await self._bot.send_message(
                chat_id=command.telegram_user_id,
                text=tr(
                    "🔐 <b>Доступ к каналу готов</b>\n\n"
                    "Нажмите зелёную кнопку ниже и отправьте запрос на вступление. "
                    "ViMi Pay подтвердит его автоматически.\n\n"
                    "⏳ Приглашение временное.",
                    "🔐 <b>Your channel access is ready</b>\n\n"
                    "Tap the button below and request to join. "
                    "ViMi Pay will approve your request automatically.\n\n"
                    "⏳ This invite is temporary.",
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text=tr("Вступить в канал", "Join channel"),
                            url=result.invite_link,
                            style="success",
                        ),
                    ]],
                ),
            )

    async def discard(self, result: TelegramAccessResult) -> None:
        if result.invite_link is None or result.resolved_chat_id is None:
            return
        await self._bot.revoke_chat_invite_link(
            chat_id=result.resolved_chat_id,
            invite_link=result.invite_link,
        )
