from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot, Router
from aiogram.exceptions import AiogramError
from aiogram.types import ChatJoinRequest, ChatMemberMember, ChatMemberUpdated, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.telegram_access.native_departures import enqueue_native_departure
from telegram_membership_saas.modules.telegram_access.service import (
    finalize_join_request_approval,
    prepare_join_request_decision,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def project_id_from_invite_name(name: str | None) -> UUID | None:
    if name is None or len(name) != 32:
        return None
    try:
        return UUID(hex=name)
    except ValueError:
        return None


async def process_chat_join_request(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    request: ChatJoinRequest,
    *,
    requested_at: datetime | None = None,
    expected_project_id: UUID | None = None,
) -> bool:
    moment = requested_at or utcnow()
    invite = request.invite_link
    project_id = project_id_from_invite_name(invite.name if invite is not None else None)

    if invite is None or project_id is None or (
        expected_project_id is not None and project_id != expected_project_id
    ):
        await bot.decline_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
        )
        return False

    async with system_project_session_scope(session_factory, project_id) as session:
        decision = await prepare_join_request_decision(
            session,
            project_id=project_id,
            invite_link=invite.invite_link,
            chat_id=request.chat.id,
            telegram_user_id=request.from_user.id,
            requested_at=moment,
        )

    if not decision.approve or decision.ticket_id is None:
        await bot.decline_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
        )
        return False

    await bot.approve_chat_join_request(
        chat_id=request.chat.id,
        user_id=request.from_user.id,
    )
    try:
        await bot.revoke_chat_invite_link(
            chat_id=request.chat.id,
            invite_link=invite.invite_link,
        )
    except AiogramError:
        pass

    async with system_project_session_scope(session_factory, project_id) as session:
        await finalize_join_request_approval(
            session,
            project_id=project_id,
            ticket_id=decision.ticket_id,
            approved_at=moment,
        )

    try:
        async with recipient_language(session_factory, request.from_user.id):
            await bot.send_message(
                chat_id=request.from_user.id,
                text=tr(
                    "✅ <b>Вы в канале!</b>\n\n"
                    "Ваша заявка на вступление одобрена. Приятного просмотра!",
                    "✅ <b>Welcome to the channel!</b>\n\n"
                    "Your join request was approved. Enjoy your membership!",
                ),
                parse_mode="HTML",
            )
    except AiogramError:
        # Membership was already granted; a notification failure must not roll it back.
        pass
    return True


async def process_native_departure(
    session_factory: async_sessionmaker[AsyncSession],
    update: ChatMemberUpdated, *, project_id: UUID, update_id: int,
) -> bool:
    old = update.old_chat_member
    new = update.new_chat_member
    if (
        update.chat.type != "channel" or not isinstance(old, ChatMemberMember)
        or old.until_date is None or old.until_date > update.date
        or new.status != "left" or new.user.id != old.user.id
    ):
        return False
    # Persist before any Telegram network read. The project worker owns
    # retries; the polling handler must not consume an event on API failure.
    async with system_project_session_scope(session_factory, project_id) as session:
        return await enqueue_native_departure(
            session, project_id=project_id, chat_id=update.chat.id,
            telegram_user_id=new.user.id, update_id=update_id,
            occurred_at=update.date,
        )


def create_telegram_access_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected_project_id: UUID | None = None,
) -> Router:
    router = Router(name="telegram-access")

    @router.chat_join_request()
    async def join_request_handler(request: ChatJoinRequest, bot: Bot) -> None:
        await process_chat_join_request(
            session_factory, bot, request, expected_project_id=expected_project_id
        )

    if expected_project_id is not None:
        project_id = expected_project_id

        @router.chat_member()
        async def native_departure_handler(
            update: ChatMemberUpdated, event_update: Update,
        ) -> None:
            await process_native_departure(
                session_factory, update, project_id=project_id, update_id=event_update.update_id,
            )

    return router
