"""Read-only integration with Telegram's native channel/group welcome editor.

Bot API 10.3 exposes can_send_welcome_messages on ChatMemberAdministrator, but
does not expose a method for reading or setting a chat's native welcome pack.
Configuration remains in the Telegram chat owner UI, never in bot credentials.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

WELCOME_PAGE_SIZE = 8
_PUBLIC_USERNAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")


@dataclass(frozen=True, slots=True)
class WelcomeTarget:
    resource_id: UUID
    name: str
    external_id: str
    resource_type: str


async def read_welcome_target(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, resource_id: UUID,
) -> WelcomeTarget:
    """Require manager permissions and ACTIVE resource in the current tenant.

    In particular, callback UUIDs from another bot cannot read a chat ID or
    influence chat permissions in the current bot.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        resource = await session.scalar(
            select(Resource).where(
                Resource.project_id == project_id,
                Resource.id == resource_id,
                Resource.status == ResourceStatus.ACTIVE.value,
            )
        )
        if resource is None:
            raise ValueError("Channel unavailable in this bot")
        return WelcomeTarget(
            resource_id=resource.id,
            name=resource.name,
            external_id=resource.external_id,
            resource_type=resource.resource_type,
        )


def telegram_chat_reference(external_id: str) -> int | str:
    """Never forward arbitrary database strings to Telegram chat methods."""
    if external_id.startswith("-") and external_id[1:].isascii() and external_id[1:].isdecimal():
        return int(external_id)
    if _PUBLIC_USERNAME.fullmatch(external_id):
        return external_id
    raise ValueError("Unsupported Telegram chat identifier")


def public_chat_url(external_id: str) -> str | None:
    """Public usernames only. Private channel deep links are not universal."""
    if _PUBLIC_USERNAME.fullmatch(external_id):
        return "https://t.me/" + external_id[1:]
    return None
