"""On-demand, read-only verification of Telegram channel creator status.

This module NEVER provisions a SaaS billing exemption. It compares the actual
registered bot identity, its single active channel and the configured
platform owner's ChatMemberOwner status. Network results are ephemeral and
are deliberately not copied into durable financial proof records.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.platform_saas_billing_service import _check_actor
from telegram_membership_saas.modules.platform_saas_payment_service import (
    platform_saas_exemption_page,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class CreatorAuditRow:
    username: str
    status: str


@dataclass(frozen=True, slots=True)
class CreatorAuditResult:
    page: int
    pages: int
    rows: tuple[CreatorAuditRow, ...]

    @property
    def creators(self) -> int:
        return sum(row.status == "creator" for row in self.rows)


async def _telegram_creator_status(
    token: str, *, expected_bot_id: int, channel_chat_id: int,
    owner_telegram_id: int,
) -> str:
    """Use Telegram as source of truth; all errors fail closed.

    No text from Telegram errors, Bot API URLs or plaintext tokens may leak
    into operator messages, database records or application logs.
    """
    bot = None
    try:
        bot = Bot(token=token)
        identity = await asyncio.wait_for(bot.get_me(), timeout=7)
        if identity.id != expected_bot_id:
            return "unavailable"
        chat = await asyncio.wait_for(
            bot.get_chat(chat_id=channel_chat_id), timeout=7,
        )
        chat_type = getattr(chat.type, "value", chat.type)
        if chat.id != channel_chat_id or chat_type != "channel":
            return "unavailable"
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=channel_chat_id, user_id=expected_bot_id),
            timeout=7,
        )
        bot_role = getattr(member.status, "value", member.status)
        if bot_role != "administrator":
            return "no_access"
        actor = await asyncio.wait_for(
            bot.get_chat_member(
                chat_id=channel_chat_id, user_id=owner_telegram_id,
            ),
            timeout=7,
        )
        actor_role = getattr(actor.status, "value", actor.status)
        if actor_role == "creator":
            return "creator"
        if actor_role == "administrator":
            return "admin_only"
        return "not_creator"
    except Exception:
        # Do not log raw exceptions from Telegram clients: some transports
        # include an access-token-bearing URL in their error message.
        return "unavailable"
    finally:
        if bot is not None:
            await bot.session.close()


async def audit_creator_page(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, actor_telegram_id: int, page: int,
    settings: Settings | None = None,
) -> CreatorAuditResult:
    """Audit only the eight pending bots on one authorized owner-only page."""
    config = settings or get_settings()
    _check_actor(actor_telegram_id, config)
    report = await platform_saas_exemption_page(
        factory, actor_user_id=actor_user_id,
        actor_telegram_id=actor_telegram_id, page=page, settings=config,
    )
    usernames = [str(item["username"]) for item in report["pending_bots"]]
    if not usernames:
        return CreatorAuditResult(
            page=page, pages=int(report["pages"]), rows=(),
        )

    # DB access is completed BEFORE any remote Bot API calls.
    # All candidates are checked against the authenticated owner and
    # the single active channel of the same project.
    # Project records are user-visible without a project setting, but
    # bot_instances and resources enforce FORCE RLS on current_project_id.
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        project_ids = tuple((await session.scalars(select(Project.id).where(
            Project.owner_user_id == actor_user_id,
        ))).all())

    candidates: dict[str, list[tuple[int, str, tuple[tuple[str, str], ...]]]] = {}
    for project_id in project_ids:
        # Never use a privileged/system session to fetch another tenant's
        # credential. The normal restricted session validates each project.
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_user_id, project_id=project_id),
        ) as session:
            bots = (await session.scalars(select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.status == "registered",
                BotInstance.username.in_(usernames),
            ))).all()
            if not bots:
                continue
            channels = (await session.scalars(select(Resource).where(
                Resource.project_id == project_id,
                Resource.status == "active",
                Resource.removed_at.is_(None),
            ))).all()
            channel_specs = tuple(
                (r.resource_type, r.external_id) for r in channels
            )
            for instance in bots:
                candidates.setdefault(instance.username, []).append((
                    instance.telegram_bot_id,
                    instance.token_ciphertext,
                    channel_specs,
                ))

    semaphore = asyncio.Semaphore(3)

    async def check(username: str) -> CreatorAuditRow:
        matched = candidates.get(username, ())
        if len(matched) != 1:
            return CreatorAuditRow(username, "needs_review")
        bot_id, ciphertext, channels = matched[0]
        if len(channels) != 1 or channels[0][0] != "telegram_channel":
            return CreatorAuditRow(username, "channel_ambiguous")
        try:
            channel_id = int(channels[0][1])
            token = decrypt_token(
                ciphertext, key=config.bot_token_encryption_key,
            )
        except Exception:
            return CreatorAuditRow(username, "unavailable")
        async with semaphore:
            result = await _telegram_creator_status(
                token, expected_bot_id=bot_id,
                channel_chat_id=channel_id,
                owner_telegram_id=actor_telegram_id,
            )
        return CreatorAuditRow(username, result)

    return CreatorAuditResult(
        page=page, pages=int(report["pages"]),
        rows=tuple(await asyncio.gather(*(check(username) for username in usernames))),
    )
