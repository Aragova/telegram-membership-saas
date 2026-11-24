from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import ChatMemberMember, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.access import AccessActionType, AccessCommand

ACTIVE_MEMBER_STATUSES = frozenset({"creator", "administrator", "member"})


class UnsafeChannelRevoke(ValueError):
    """The member cannot safely be removed by automated enforcement."""


@dataclass(frozen=True, slots=True)
class TelegramAccessCommand:
    access: AccessCommand
    telegram_user_id: int
    chat_id: int | str
    is_channel: bool = False
    native_channel_history: bool = False


@dataclass(frozen=True, slots=True)
class TelegramAccessResult:
    invite_link: str | None = None
    invite_expires_at: datetime | None = None
    resolved_chat_id: int | None = None
    already_member: bool = False
    revoke_retry_at: datetime | None = None


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
    def bot(self) -> Bot:
        """Bot handle for project-bound notification delivery."""
        return self._bot

    @property
    def invite_ttl(self) -> timedelta:
        return self._invite_ttl

    async def prepare(self, command: TelegramAccessCommand) -> TelegramAccessResult:
        if command.access.action_type == AccessActionType.GRANT:
            return await self._prepare_grant(command)
        if command.access.action_type == AccessActionType.REVOKE:
            if command.is_channel:
                return await self._prepare_channel_revoke(command)
            await self._bot.ban_chat_member(
                chat_id=command.chat_id,
                user_id=command.telegram_user_id,
                revoke_messages=False,
            )
            return TelegramAccessResult()
        raise ValueError(f"Unsupported access action: {command.access.action_type}")

    async def _prepare_channel_revoke(
        self, command: TelegramAccessCommand,
    ) -> TelegramAccessResult:
        # The optional subscription timestamp belongs to ChatMemberMember
        # only: the same field on a banned/restricted member is a ban date.
        member = await self._bot.get_chat_member(
            chat_id=command.chat_id, user_id=command.telegram_user_id,
        )
        if isinstance(member, ChatMemberMember) and member.until_date is not None:
            now = self._clock()
            # Even a passed timestamp is not permission to kick a member:
            # Telegram owns native expiry/renewal, including its races.
            retry_at = max(now + timedelta(minutes=5), member.until_date)
            return TelegramAccessResult(
                revoke_retry_at=min(retry_at, now + timedelta(hours=1)),
            )
        status = getattr(member.status, "value", member.status)
        if status in ("left", "kicked"):
            # No new ban (and no unban of an administrator's existing ban).
            return TelegramAccessResult()
        if status != "member":
            # Never try to demote an administrator or guess an unknown state.
            raise UnsafeChannelRevoke("Channel membership cannot be safely revoked")
        if not isinstance(member, ChatMemberMember):
            raise UnsafeChannelRevoke("Unrecognized channel member representation")

        if command.native_channel_history:
            # Ordinary expiry must not create an indefinite ban that blocks
            # the next Telegram-paid join. Telegram provides no atomic
            # conditional remove; the membership read above is mandatory.
            # Bot API explicitly guarantees removal with only_if_banned=False:
            # https://core.telegram.org/bots/api#unbanchatmember
            await self._bot.unban_chat_member(
                chat_id=command.chat_id, user_id=command.telegram_user_id,
                only_if_banned=False,
            )
        else:
            await self._bot.ban_chat_member(
                chat_id=command.chat_id, user_id=command.telegram_user_id,
                revoke_messages=False,
            )
        return TelegramAccessResult()

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
        if result.invite_link is None and not result.already_member:
            return
        async with recipient_language(self._session_factory, command.telegram_user_id):
            if result.already_member:
                await self._bot.send_message(
                    chat_id=command.telegram_user_id,
                    text=tr(
                        "✅ <b>Доступ к каналу подтверждён!</b>\n\n"
                        "Вы уже состоите в этом приватном канале, поэтому "
                        "повторное приглашение не требуется.",
                        "✅ <b>Channel access confirmed!</b>\n\n"
                        "You are already a member of this private channel, "
                        "so you do not need another invitation.",
                    ),
                    parse_mode="HTML",
                )
                return
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
