"""Owner-initiated, non-lossy bot retirement.

Retirement stops buyer polling before deleting Fernet credentials; project
configuration and financial history are not tied to BotInstance identities.
In-flight access/payment obligations block retirement until resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope, tenant_session_scope
from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.entitlements.models import EntitlementGrant
from telegram_membership_saas.modules.payments.models import PaymentRequest, PaymentRequestStatus
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions.models import Subscription
from telegram_membership_saas.modules.telegram_access.models import TelegramJoinTicket
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class RetirementBlockers:
    requests: int = 0
    subscriptions: int = 0
    grants: int = 0
    access: int = 0
    access_actions: int = 0
    join_tickets: int = 0

    @property
    def has_blockers(self) -> bool:
        return any((
            self.requests, self.subscriptions, self.grants, self.access,
            self.access_actions, self.join_tickets,
        ))

    def display(self) -> str:
        labels = (
            ("незавершённых оплат", self.requests),
            ("действующих подписок", self.subscriptions),
            ("действующих прав доступа", self.grants),
            ("неотозванных доступов", self.access),
            ("незавершённых действий доступа", self.access_actions),
            ("неиспользованных приглашений", self.join_tickets),
        )
        return ", ".join(f"{label}: {n}" for label, n in labels if n)


async def _count(session: AsyncSession, model, *conditions) -> int:
    return int((await session.scalar(
        select(func.count()).select_from(model).where(*conditions)
    )) or 0)


async def count_retirement_blockers(
    session: AsyncSession, *, project_id: UUID,
) -> RetirementBlockers:
    now = datetime.now(UTC)
    return RetirementBlockers(
        requests=await _count(
            session, PaymentRequest,
            PaymentRequest.project_id == project_id,
            PaymentRequest.status.in_((
                PaymentRequestStatus.WAITING_CONFIRMATION.value,
                PaymentRequestStatus.WAITING_REVIEW.value,
            )),
        ),
        subscriptions=await _count(
            session, Subscription,
            Subscription.project_id == project_id,
            Subscription.status == "active",
            or_(
                Subscription.current_period_end.is_(None),
                Subscription.current_period_end > now,
            ),
        ),
        grants=await _count(
            session, EntitlementGrant,
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.status == "active",
            or_(
                EntitlementGrant.valid_until.is_(None),
                EntitlementGrant.valid_until > now,
            ),
        ),
        access=await _count(
            session, AccessState,
            AccessState.project_id == project_id,
            or_(
                AccessState.desired_access.is_(True),
                AccessState.actual_state == "granted",
            ),
        ) + await _count(
            session, EffectiveAccess,
            EffectiveAccess.project_id == project_id,
            EffectiveAccess.has_access.is_(True),
        ),
        access_actions=await _count(
            session, AccessAction,
            AccessAction.project_id == project_id,
            AccessAction.status.in_((
                "pending", "processing", "failed_retryable",
            )),
        ),
        join_tickets=await _count(
            session, TelegramJoinTicket,
            TelegramJoinTicket.project_id == project_id,
            TelegramJoinTicket.status == "pending",
        ),
    )


async def request_bot_retirement(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: UUID,
    project_id: UUID,
    bot_instance_id: UUID,
) -> bool:
    """Mark for worker drain only after ownership/obligations are verified.

    A registered bot can only be removed by the actual project owner, not
    management-role collaborators. Serialize with registration via FOR UPDATE.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        # Serialize with every new checkout so a payment accepted during
        # deletion confirmation cannot escape the blockers check.
        await checkout_retirement_lock(session, project_id)
        project = await session.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is None or project.owner_user_id != actor_id:
            raise PermissionError("Удалить бота может только владелец проекта")
        bot = await session.scalar(
            select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.id == bot_instance_id,
            ).with_for_update()
        )
        if bot is None:
            return False
        if bot.status == BotInstanceStatus.DELETING.value:
            return True
        blockers = await count_retirement_blockers(session, project_id=project_id)
        if blockers.has_blockers:
            raise ValueError(
                "Удаление пока невозможно: " + blockers.display()
                + ". Сначала завершите оплаты и корректно отзовите "
                "доступ подписчиков. Финансовая история сохраняется."
            )
        bot.status = BotInstanceStatus.DELETING.value
        # Preserve the encrypted credential until the supervisor confirms
        # polling stopped and has retired this exact identity.
        await session.flush()
        return True


async def finalize_bot_retirement(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    telegram_bot_id: int,
) -> bool:
    """Trusted supervisor only: caller MUST hold project advisory lock."""
    async with system_project_session_scope(factory, project_id) as session:
        bot = await session.scalar(
            select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.telegram_bot_id == telegram_bot_id,
                BotInstance.status == BotInstanceStatus.DELETING.value,
            ).with_for_update()
        )
        if bot is None:
            return False
        # Never delete a BotInstance if an in-flight payment or grant became
        # visible after the owner clicked confirm.
        blockers = await count_retirement_blockers(session, project_id=project_id)
        if blockers.has_blockers:
            # A checkout already in flight might have committed after the
            # request. Restore this bot so ongoing access work can continue;
            # the owner can retry after all obligations are settled.
            bot.status = BotInstanceStatus.REGISTERED.value
            await session.flush()
            return False
        await session.delete(bot)
        await session.flush()
        return True
