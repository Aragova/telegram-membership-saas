"""Opt-in paid channel invite links; channel, not bot, receives the Stars.

This module does NOT treat a paid invitation as a confirmed ViMi Pay order.
Bot API does not provide channel transaction receipts through a bot token.
Telegram renews the channel subscription and controls channel membership.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.plans import Plan, PlanResource, PlanStatus
from telegram_membership_saas.modules.pricing import PlanPrice, PlanPriceStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

NATIVE_PERIOD_SECONDS = 2_592_000
NATIVE_MAX_STARS = 10_000


def native_price_allowed(
    *, duration_value: int | None, duration_unit: str, amount: Decimal,
) -> bool:
    """Telegram's 30-day subscriptions accept 1..10000 whole Stars."""
    return (
        duration_value == 1 and duration_unit == "month"
        and amount == amount.to_integral_value()
        and 1 <= amount <= NATIVE_MAX_STARS
    )


@dataclass(frozen=True, slots=True)
class NativeStarsTarget:
    plan_id: UUID
    price_id: UUID
    resource_id: UUID
    chat_id: int
    amount: int
    bot_id: int
    invite_link: str | None
    active: bool


async def native_commerce_state(
    session: AsyncSession, *, project_id: UUID, bot_instance_id: UUID,
) -> str:
    """Use the same live platform gate as bot Stars checkout and admin billing."""
    state = await session.scalar(
        text(
            "SELECT public.platform_saas_project_commerce_state("
            ":project,:bot)"
        ),
        {"project": project_id, "bot": bot_instance_id},
    )
    return str(state)


async def _target(
    session: AsyncSession, *, project_id: UUID, plan_id: UUID, bot_id: int,
    for_update: bool = False,
) -> NativeStarsTarget:
    registered = await session.scalar(select(BotInstance.id).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
        BotInstance.status == BotInstanceStatus.REGISTERED.value,
    ))
    if registered is None:
        raise PermissionError("This bot does not belong to the active project")
    plan_stmt = select(Plan).where(
        Plan.id == plan_id, Plan.project_id == project_id,
        Plan.status == PlanStatus.ACTIVE.value, Plan.removed_at.is_(None),
    )
    if for_update:
        plan_stmt = plan_stmt.with_for_update()
    plan = await session.scalar(plan_stmt)
    if plan is None:
        raise ValueError("The subscription plan is no longer active")
    price_stmt = select(PlanPrice).where(
        PlanPrice.project_id == project_id,
        PlanPrice.plan_id == plan_id,
        PlanPrice.currency_code == "XTR",
        PlanPrice.status == PlanPriceStatus.ACTIVE.value,
    )
    if for_update:
        price_stmt = price_stmt.with_for_update()
    price = await session.scalar(price_stmt)
    if price is None or not native_price_allowed(
        duration_value=plan.duration_value,
        duration_unit=plan.duration_unit,
        amount=price.amount if price is not None else Decimal(0),
    ):
        raise ValueError(
            "Channel Stars require a 1-month plan priced at 1–10000 whole Stars"
        )
    if not price.native_subscription_active:
        state = await native_commerce_state(
            session, project_id=project_id, bot_instance_id=registered,
        )
        if state not in ("clear", "disabled"):
            # A payment hold cannot be bypassed by moving the charge onto
            # the channel. Unknown billing state must also fail closed.
            raise PermissionError(
                "Новые продажи ⭐ Telegram Stars заблокированы "
                "ограничениями обслуживания ViMi Pay."
            )
    resources = (await session.scalars(
        select(Resource).join(PlanResource, (
            Resource.project_id == PlanResource.project_id
        ) & (
            Resource.id == PlanResource.resource_id
        )).where(
            PlanResource.project_id == project_id,
            PlanResource.plan_id == plan_id,
        )
    )).all()
    if (
        len(resources) != 1
        or resources[0].resource_type != ResourceType.TELEGRAM_CHANNEL.value
        or resources[0].status != ResourceStatus.ACTIVE.value
        or resources[0].removed_at is not None
    ):
        raise ValueError(
            "Channel Stars require exactly one active private channel per plan"
        )
    try:
        chat_id = int(resources[0].external_id)
    except ValueError as exc:
        raise ValueError("Channel must have a numeric Telegram chat ID") from exc
    if price.native_subscription_active and (
        price.native_subscription_chat_id != resources[0].external_id
        or price.native_subscription_bot_id != bot_id
    ):
        raise ValueError("Native Stars link belongs to another channel or bot")
    return NativeStarsTarget(
        plan_id=plan_id, price_id=price.id, resource_id=resources[0].id,
        chat_id=chat_id, amount=int(price.amount), bot_id=bot_id,
        invite_link=price.native_subscription_link,
        active=bool(price.native_subscription_active),
    )


async def read_native_stars_target(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID, bot_id: int,
) -> NativeStarsTarget:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        return await _target(
            session, project_id=project_id, plan_id=plan_id, bot_id=bot_id,
        )


async def activate_native_stars_link(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID, bot_id: int,
    expected_price_id: UUID, invite_link: str,
) -> None:
    """Commit only if the verified plan, channel, price and bot still match."""
    if not invite_link.startswith(("https://t.me/+", "https://t.me/joinchat/")):
        raise ValueError("Telegram did not return a valid paid invitation URL")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        target = await _target(
            session, project_id=project_id, plan_id=plan_id,
            bot_id=bot_id, for_update=True,
        )
        if target.price_id != expected_price_id or target.active:
            raise ValueError("The native Stars configuration has changed")
        price = await session.get(PlanPrice, target.price_id)
        price.native_subscription_link = invite_link
        price.native_subscription_chat_id = str(target.chat_id)
        price.native_subscription_bot_id = bot_id
        price.native_subscription_active = True
        await session.flush()


async def deactivate_native_stars_link(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID, bot_id: int,
) -> tuple[int, str] | None:
    """Hide from NEW buyer checkouts before revoking the external Telegram link.

    Revoking an invite does not make a historical payment disappear, nor does
    this method claim to cancel existing native Telegram subscriptions.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        # Deactivation uses the immutable Telegram link snapshot, not the
        # currently active plan/channel. Owners must be able to stop sales
        # even after a plan was archived or its linked resource changed.
        registered = await session.scalar(select(BotInstance.id).where(
            BotInstance.project_id == project_id,
            BotInstance.telegram_bot_id == bot_id,
            BotInstance.status == BotInstanceStatus.REGISTERED.value,
        ))
        if registered is None:
            raise PermissionError("This bot does not belong to the active project")
        price = await session.scalar(
            select(PlanPrice).where(
                PlanPrice.project_id == project_id,
                PlanPrice.plan_id == plan_id,
                PlanPrice.currency_code == "XTR",
                PlanPrice.native_subscription_active.is_(True),
                PlanPrice.native_subscription_bot_id == bot_id,
            ).order_by(PlanPrice.version.desc()).limit(1).with_for_update()
        )
        if price is None:
            return None
        # Snapshot fields are constrained together by migration 0095.
        chat_id = int(price.native_subscription_chat_id)
        invite_link = price.native_subscription_link
        price.native_subscription_active = False
        await session.flush()
        return chat_id, invite_link
