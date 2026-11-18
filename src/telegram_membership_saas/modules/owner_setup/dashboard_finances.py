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
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)
MAX_CHANNELS_ON_DASHBOARD = 3
TELEGRAM_READ_TIMEOUT = 4


@dataclass(frozen=True, slots=True)
class ChannelFinance:
    name: str
    chat_id: str
    owner_label: str | None
    owner_deleted: bool
    stars_recipient: str  # bot, channel, mixed, unconfigured
    owner_username: str | None = None
    owner_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class DashboardFinances:
    bot_stars: str | None
    channels: tuple[ChannelFinance, ...]
    extra_channels: int = 0
    available: bool = True


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
        channels = (await session.execute(select(
            Resource.id, Resource.name, Resource.external_id,
        ).where(
            Resource.project_id == project_id,
            Resource.resource_type == "telegram_channel",
            Resource.status == "active", Resource.removed_at.is_(None),
        ).order_by(Resource.name, Resource.id))).all()
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
    results = await asyncio.gather(
        _live_stars(bot),
        *(_live_owner(bot, chat.external_id) for chat in visible),
    )
    bot_balance = results[0]
    cards = []
    for channel, ownership in zip(visible, results[1:], strict=True):
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
            owner_user_id=ownership.user_id,
        ))
    return DashboardFinances(
        bot_stars=bot_balance,
        channels=tuple(cards),
        extra_channels=max(0, len(channels) - len(cards)),
    )
