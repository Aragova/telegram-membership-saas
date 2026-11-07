"""One-to-one support replies inside a connected project bot.

The original Telegram bot message is the reply authorization marker. A
short command is a navigation reference, never a bearer credential.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.identity.models import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.owner_setup.customer_shortcodes import (
    customer_command,
    parse_customer_command,
)
from telegram_membership_saas.modules.projects.models import Project, ProjectStatus

ADMIN_MESSAGE_HEADER = "✉️ ViMi Pay · Сообщение администратора"
ADMIN_MESSAGE_HEADER_EN = "✉️ ViMi Pay · Message from admin"
BUYER_REPLY_PROMPT = "✍️ ViMi Pay · Ответ администратору"
BUYER_REPLY_PROMPT_EN = "✍️ ViMi Pay · Reply to admin"
BUYER_MESSAGE_HEADER = "💬 ViMi Pay · Покупатель "
BUYER_MESSAGE_HEADER_EN = "💬 ViMi Pay · Customer "
MAX_DIALOG_TEXT = 3500


@dataclass(frozen=True, slots=True)
class BuyerConversation:
    customer_id: UUID
    customer_telegram_id: int
    username: str | None
    first_name: str | None
    owner_telegram_id: int
    is_banned: bool


def is_admin_message(text: str | None) -> bool:
    """Check an original bot-sent message's first line, not user content."""
    return bool(text and text.split("\n", 1)[0] in (
        ADMIN_MESSAGE_HEADER, ADMIN_MESSAGE_HEADER_EN,
        BUYER_REPLY_PROMPT, BUYER_REPLY_PROMPT_EN,
    ))


def parse_buyer_notification(text: str | None) -> int:
    """Extract the buyer's ID only from our bot-sent first-line marker."""
    first = (text or "").split("\n", 1)[0]
    prefix = next((
        marker for marker in (BUYER_MESSAGE_HEADER, BUYER_MESSAGE_HEADER_EN)
        if first.startswith(marker)
    ), None)
    if prefix is None:
        raise ValueError("This is not a buyer conversation notification")
    telegram_id, customer_uuid = parse_customer_command(
        first[len(prefix):],
    )
    if telegram_id is None or customer_uuid is not None:
        raise ValueError("Invalid buyer message reference")
    return telegram_id


def customer_ref(info: BuyerConversation) -> str:
    return customer_command(
        telegram_id=info.customer_telegram_id, customer_id=info.customer_id,
    )


async def find_buyer_conversation(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, telegram_id: int,
) -> BuyerConversation | None:
    """Resolve a customer and their project's owner, never across projects."""
    if telegram_id <= 0:
        return None
    async with system_project_session_scope(factory, project_id) as session:
        owner_user = TelegramUser.__table__.alias("owner_telegram_user")
        row = (await session.execute(
            select(
                ProjectCustomer, TelegramUser,
                owner_user.c.telegram_user_id,
            )
            .join(
                TelegramUser,
                TelegramUser.id == ProjectCustomer.user_id,
            )
            .join(
                Project,
                Project.id == ProjectCustomer.project_id,
            )
            .join(
                owner_user,
                owner_user.c.id == Project.owner_user_id,
            )
            .where(
                ProjectCustomer.project_id == project_id,
                TelegramUser.telegram_user_id == telegram_id,
                Project.status == ProjectStatus.ACTIVE.value,
            )
        )).one_or_none()
        if row is None:
            return None
        customer, user, owner_telegram_id = row
        return BuyerConversation(
            customer_id=customer.id,
            customer_telegram_id=user.telegram_user_id,
            username=user.username,
            first_name=user.first_name,
            owner_telegram_id=owner_telegram_id,
            is_banned=customer.is_banned,
        )
