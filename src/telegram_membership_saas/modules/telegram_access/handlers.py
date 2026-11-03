from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot, Router
from aiogram.exceptions import AiogramError
from aiogram.types import ChatJoinRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
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
) -> bool:
    moment = requested_at or utcnow()
    invite = request.invite_link
    project_id = project_id_from_invite_name(invite.name if invite is not None else None)

    if invite is None or project_id is None:
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
        await bot.send_message(
            chat_id=request.from_user.id,
            text=(
                "✅ <b>Вы в канале!</b>\n\n"
                "Ваша заявка на вступление одобрена. Приятного просмотра!"
            ),
            parse_mode="HTML",
        )
    except AiogramError:
        # Membership was already granted; a notification failure must not roll it back.
        pass
    return True


def create_telegram_access_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="telegram-access")

    @router.chat_join_request()
    async def join_request_handler(request: ChatJoinRequest, bot: Bot) -> None:
        await process_chat_join_request(session_factory, bot, request)

    return router
