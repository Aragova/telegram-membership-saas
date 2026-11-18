"""Read-only live Telegram Stars / channel ownership for the owner dashboard.

Bot API only discloses the connected bot's Stars balance. It does not disclose
the Telegram channel's revenue balance, withdrawal eligibility, or channel
transactions. Never substitute the bot's balance or subscriber count.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import AiogramError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.channel_revenue_bridge import (
    ChannelRevenue,
    live_channel_revenue,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)
MAX_CHANNELS_ON_DASHBOARD = 3
TELEGRAM_READ_TIMEOUT = 4
MAX_RESOURCE_ACCESS_CHECKS = 8


@dataclass(frozen=True, slots=True)
class ChannelFinance:
    name: str
    chat_id: str
    owner_label: str | None
    owner_deleted: bool
    stars_recipient: str  # bot, channel, mixed, unconfigured
    owner_username: str | None = None
    owner_user_id: int | None = None
    revenue: ChannelRevenue | None = None


@dataclass(frozen=True, slots=True)
class DashboardFinances:
    bot_stars: str | None
    channels: tuple[ChannelFinance, ...]
    extra_channels: int = 0
    available: bool = True
    channel_ready: bool = False


def _format_stars_amount(amount: object) -> str:
    whole = int(amount.amount)
    nanos = int(getattr(amount, "nanostar_amount", 0) or 0)
    if abs(nanos) >= 1_000_000_000:
        raise ValueError("Invalid fractional Telegram Stars amount")
    value = Decimal(whole) + Decimal(nanos) / Decimal(1_000_000_000)
    return format(value, "f").rstrip("0").rstrip(".") if nanos else str(whole)


@dataclass(frozen=True, slots=True)
class ChannelOwner:
    label: str | None
    deleted: bool = False
    username: str | None = None
    user_id: int | None = None


def _owner_details_from_admins(admins: object) -> ChannelOwner:
    """Only a Telegram creator is an owner; admins are not interchangeable."""
    for member in admins:
        if getattr(member, "status", None) != "creator":
            continue
        user = member.user
        deleted = bool(getattr(user, "is_deleted", False))
        user_id = getattr(user, "id", None)
        if deleted:
            return ChannelOwner(
                label="ID " + str(user_id) if user_id is not None else None,
                deleted=True, user_id=user_id,
            )
        username = getattr(user, "username", None)
        label = (
            "@" + username if username else
            " ".join(filter(None, (
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            ))) or None
        )
        # Keep the numeric user ID as evidence, not as UI copy.
        legacy_label = (
            (label + " · " if label else "") + "ID " + str(user_id)
            if user_id is not None else label
        )
        return ChannelOwner(
            label=legacy_label, username=username, user_id=user_id,
        )
    return ChannelOwner(label=None)


def _owner_from_admins(admins: object) -> tuple[str | None, bool]:
    """Compatible helper used by existing ownership contract tests."""
    result = _owner_details_from_admins(admins)
    return result.label, result.deleted


def _revenue_actor_matches_creator(
    actor_telegram_id: int | None, ownership: ChannelOwner,
) -> bool:
    """Project access alone never permits channel-owner financial disclosure."""
    return bool(
        actor_telegram_id is not None
        and actor_telegram_id > 0
        and not ownership.deleted
        and ownership.user_id == actor_telegram_id
    )


async def _live_stars(bot: Bot) -> str | None:
    try:
        amount = await asyncio.wait_for(
            bot.get_my_star_balance(), timeout=TELEGRAM_READ_TIMEOUT,
        )
        return _format_stars_amount(amount)
    except (AiogramError, TimeoutError, AttributeError, TypeError, ValueError):
        log.info("Bot Telegram Stars balance unavailable for dashboard")
        return None


async def _live_owner(bot: Bot, chat_id: str) -> ChannelOwner:
    try:
        admins = await asyncio.wait_for(
            bot.get_chat_administrators(chat_id=int(chat_id)),
            timeout=TELEGRAM_READ_TIMEOUT,
        )
        return _owner_details_from_admins(admins)
    except (AiogramError, TimeoutError, AttributeError, TypeError, ValueError):
        log.info("Telegram owner unavailable for channel %s", chat_id)
        return ChannelOwner(label=None)


async def _live_resource_rights(bot: Bot, chat_id: str) -> bool:
    """Check current Telegram rights, not a stale active Resource row."""
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=int(chat_id), user_id=bot.id),
            timeout=TELEGRAM_READ_TIMEOUT,
        )
        if member.status == "creator":
            return True
        return bool(
            member.status == "administrator"
            and getattr(member, "can_invite_users", False)
            and getattr(member, "can_restrict_members", False)
        )
    except (AiogramError, TimeoutError, TypeError, AttributeError, ValueError):
        log.info("Telegram bot resource rights unavailable: %s", chat_id)
        return False


async def read_dashboard_finances(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, bot: Bot,
) -> DashboardFinances:
    """One authorized tenant read, then bounded concurrent Telegram calls.

    For large projects, show only the first three channels in the dashboard;
    every channel remains reachable via the existing 'Channels' menu.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        project_owner_id = await session.scalar(
            select(Project.owner_user_id).where(Project.id == project_id)
        )
        # The project owner is not necessarily the Telegram channel creator:
        # project setup explicitly permits connecting as channel admin.
        # Revenue must never leak across these two different identities.
        actor_telegram_id = None
        if project_owner_id == actor_id:
            actor_telegram_id = await session.scalar(
                select(TelegramUser.telegram_user_id).where(
                    TelegramUser.id == actor_id,
                )
            )
        channels = (await session.execute(select(
            Resource.id, Resource.name, Resource.external_id,
        ).where(
            Resource.project_id == project_id,
            Resource.resource_type == "telegram_channel",
            Resource.status == "active", Resource.removed_at.is_(None),
        ).order_by(Resource.name, Resource.id))).all()
        # The setup screen previously trusted a stored Resource.status
        # despite revoked or changed Telegram admin permissions. Validate
        # the current rights for all normally connected resources.
        active_resource_chats = (await session.scalars(
            select(Resource.external_id).where(
                Resource.project_id == project_id,
                Resource.resource_type.in_(
                    ("telegram_channel", "telegram_group"),
                ),
                Resource.status == "active",
                Resource.removed_at.is_(None),
            ).order_by(Resource.id)
        )).all()
        # Each active Stars price points to one or more current resources.
        # This distinguishes the native paid channel link from old bot invoices.
        stars = (await session.execute(select(
            PlanResource.resource_id,
            PlanPrice.native_subscription_active,
            PlanPrice.native_subscription_chat_id,
        ).join(
            Plan, and_(
                Plan.id == PlanResource.plan_id,
                Plan.project_id == PlanResource.project_id,
            ),
        ).join(
            PlanPrice, and_(
                PlanPrice.plan_id == Plan.id,
                PlanPrice.project_id == Plan.project_id,
            ),
        ).where(
            PlanResource.project_id == project_id,
            Plan.status == "active", Plan.removed_at.is_(None),
            PlanPrice.currency_code == "XTR",
            PlanPrice.status == "active",
        ))).all()
    routes: dict[UUID, set[str]] = {}
    chat_by_id = {row.id: row.external_id for row in channels}
    for resource_id, native, linked_chat in stars:
        if native and linked_chat == chat_by_id.get(resource_id):
            route = "channel"
        elif native:
            # A stale native link must not be advertised as a bot invoice.
            continue
        else:
            route = "bot"
        routes.setdefault(resource_id, set()).add(route)

    visible = channels[:MAX_CHANNELS_ON_DASHBOARD]
    # An unverified resource must not display green ON. All Telegram calls
    # run concurrently to avoid doubling main-panel latency.
    verify_resources = 0 < len(active_resource_chats) <= MAX_RESOURCE_ACCESS_CHECKS
    results = await asyncio.gather(
        _live_stars(bot),
        *(_live_owner(bot, chat.external_id) for chat in visible),
        *(
            _live_resource_rights(bot, chat_id)
            for chat_id in (active_resource_chats if verify_resources else ())
        ),
    )
    bot_balance = results[0]
    owner_results = results[1:1 + len(visible)]
    channel_ready = (
        all(results[1 + len(visible):]) if verify_resources else False
    )
    # Channel finances are sensitive. Even project moderators who can view
    # the regular dashboard do not receive MTProto revenue information.
    # The isolated bridge also verifies the precise Telegram creator ID.
    async def revenue_for(channel, ownership) -> ChannelRevenue | None:
        if not _revenue_actor_matches_creator(actor_telegram_id, ownership):
            return None
        try:
            return await live_channel_revenue(
                chat_id=int(channel.external_id), owner_id=ownership.user_id,
            )
        except (ValueError, TypeError):
            return None

    revenue_results = await asyncio.gather(*(
        revenue_for(channel, ownership)
        for channel, ownership in zip(visible, owner_results, strict=True)
    ))
    cards = []
    for channel, ownership, revenue in zip(
        visible, owner_results, revenue_results, strict=True,
    ):
        available = routes.get(channel.id, set())
        payout = (
            "mixed" if len(available) > 1 else
            next(iter(available)) if available else "unconfigured"
        )
        cards.append(ChannelFinance(
            name=channel.name, chat_id=channel.external_id,
            owner_label=ownership.label, owner_deleted=ownership.deleted,
            stars_recipient=payout,
            owner_username=ownership.username,
            owner_user_id=ownership.user_id, revenue=revenue,
        ))
    return DashboardFinances(
        bot_stars=bot_balance,
        channels=tuple(cards),
        extra_channels=max(0, len(channels) - len(cards)),
        channel_ready=channel_ready,
    )
