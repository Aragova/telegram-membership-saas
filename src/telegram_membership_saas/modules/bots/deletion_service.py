"""Owner-initiated, non-lossy bot retirement.

Retirement stops buyer polling before deleting Fernet credentials; project
configuration and financial history are not tied to BotInstance identities.
In-flight access/payment obligations block retirement until resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope, tenant_session_scope
from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessActionStatus,
    AccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.entitlements.models import EntitlementGrant
from telegram_membership_saas.modules.file_sales.manual_models import FileManualOrder
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.payments.models import PaymentRequest, PaymentRequestStatus
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.stars_subscriptions import SubscriptionStarOrder
from telegram_membership_saas.modules.subscriptions.models import Subscription
from telegram_membership_saas.modules.telegram_access.models import TelegramJoinTicket
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    checkout_retirement_lock,
    project_worker_lock,
)
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class RetirementBlockers:
    requests: int = 0
    file_orders: int = 0
    stars_checkouts: int = 0
    subscriptions: int = 0
    grants: int = 0
    access: int = 0
    access_actions: int = 0
    join_tickets: int = 0

    @property
    def has_blockers(self) -> bool:
        return any((
            self.requests, self.file_orders, self.stars_checkouts,
            self.subscriptions, self.grants,
            self.access, self.access_actions, self.join_tickets,
        ))

    def display(self) -> str:
        labels = (
            ("незавершённых оплат", self.requests),
            ("незавершённых оплат файлов", self.file_orders),
            ("Stars-оплат с возможным списанием", self.stars_checkouts),
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


def owner_self_managed_access_predicate():
    """Owner's own membership may outlive bot credentials.

    Telegram bots cannot ban channel creators or administrators. This only
    exempts the actual *project owner's* membership after all desired access
    and projected entitlements for that target are gone. It does not change
    actual_state, bypass paid grants, or exempt any customer.
    """
    owner_without_entitlement = (
        select(1)
        .select_from(ProjectCustomer)
        .join(
            Project,
            and_(
                Project.id == ProjectCustomer.project_id,
                Project.owner_user_id == ProjectCustomer.user_id,
            ),
        )
        .join(
            EffectiveAccess,
            and_(
                EffectiveAccess.project_id == ProjectCustomer.project_id,
                EffectiveAccess.customer_id == ProjectCustomer.id,
                EffectiveAccess.resource_id == AccessState.resource_id,
            ),
        )
        .where(
            ProjectCustomer.project_id == AccessState.project_id,
            ProjectCustomer.id == AccessState.customer_id,
            EffectiveAccess.has_access.is_(False),
        )
        .correlate(AccessState)
        .exists()
    )
    return and_(AccessState.desired_access.is_(False), owner_without_entitlement)


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
        file_orders=await _count(
            session, FileManualOrder,
            FileManualOrder.project_id == project_id,
            FileManualOrder.status.in_(("pending", "submitted")),
        ),
        stars_checkouts=await _count(
            session, SubscriptionStarOrder,
            SubscriptionStarOrder.project_id == project_id,
            SubscriptionStarOrder.status == "pending",
            or_(
                SubscriptionStarOrder.created_at > now - timedelta(minutes=40),
                SubscriptionStarOrder.precheckout_approved_at
                > now - timedelta(hours=24),
            ),
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
            ~owner_self_managed_access_predicate(),
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


async def retire_unreachable_bot_with_orphan_actions(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, bot_instance_id: UUID,
) -> int:
    """Explicit owner emergency retirement when ONLY undeliverable actions remain.

    The BotFather token can no longer run in this situation. Never pretend
    Telegram granted/revoked membership: preserve AccessState and historical
    AccessAction rows and audit the owner's decision. Do NOT bypass any paid
    customer obligation or a live bot/worker. Set DELETING atomically so the
    supervisor performs its normal poller shutdown and credential retirement.
    """
    # Acquire the SAME advisory lease as the customer bot runtime, not the
    # unrelated checkout/retirement transaction lock. Never abandon a command
    # while an active poller may still execute Telegram side effects.
    try:
        async with project_worker_lock(factory.kw["bind"], project_id):
            now = datetime.now(UTC)
            async with tenant_session_scope(
                factory, TenantContext(user_id=actor_id, project_id=project_id),
            ) as session:
                await checkout_retirement_lock(session, project_id)
                project = await session.scalar(
                    select(Project).where(Project.id == project_id).with_for_update()
                )
                if project is None or project.owner_user_id != actor_id:
                    raise PermissionError("Отключить недоступного бота может только владелец")
                if project.status != "active":
                    raise ValueError("Подключение уже не активно")
                bot = await session.scalar(
                    select(BotInstance).where(
                        BotInstance.id == bot_instance_id,
                        BotInstance.project_id == project_id,
                    ).with_for_update()
                )
                if bot is None or bot.status == BotInstanceStatus.DELETING.value:
                    raise ValueError("Бот уже отключён или удаляется. Обновите статус")

                blockers = await count_retirement_blockers(session, project_id=project_id)
                if (
                    blockers.requests or blockers.file_orders
                    or blockers.stars_checkouts
                    or blockers.subscriptions or blockers.grants
                    or blockers.access or blockers.join_tickets
                ):
                    raise ValueError(
                        "Есть обязательства перед покупателями или неотозванный доступ. "
                        "Экстренное отключение запрещено: " + blockers.display()
                    )
                if not blockers.access_actions:
                    raise ValueError(
                        "Незавершённых действий доступа больше нет. "
                        "Отключите бота обычной кнопкой."
                    )

                health = await session.scalar(
                    select(CustomerBotRuntimeHealth).where(
                        CustomerBotRuntimeHealth.project_id == project_id,
                    )
                )
                if (
                    health is not None
                    and health.telegram_bot_id == bot.telegram_bot_id
                    and health.state == "running"
                    and health.last_heartbeat >= now - timedelta(seconds=60)
                ):
                    raise ValueError(
                        "Бот сейчас работает. Используйте обычное отключение "
                        "после завершения действий доступа."
                    )

                actions = list((await session.scalars(
                    select(AccessAction).where(
                        AccessAction.project_id == project_id,
                        AccessAction.status.in_((
                            AccessActionStatus.PENDING.value,
                            AccessActionStatus.PROCESSING.value,
                            AccessActionStatus.FAILED_RETRYABLE.value,
                        )),
                    ).order_by(AccessAction.created_at, AccessAction.id).with_for_update()
                )).all())
                if len(actions) != blockers.access_actions:
                    raise ValueError("Действия доступа изменились. Обновите обязательства")
                # Holding the EXACT project worker advisory lease proves the customer
                # poller is not running, including during an external Telegram API
                # call. Unlike an action timestamp, this fences in-flight operations.
                # A crashed/stale processing claim is therefore safe to abandon.

                for action in actions:
                    action.status = AccessActionStatus.SUPERSEDED.value
                    action.locked_at = None
                    action.processed_at = now
                    session.add(OwnerObligationAudit(
                        project_id=project_id, actor_id=actor_id,
                        action="abandon_orphan_access_action",
                        target_id=action.id,
                        reason=(
                            "Owner explicitly retired an unreachable bot with no "
                            "live payment, subscription, grant, access or invite "
                            "obligations; Telegram physical state NOT verified."
                        ),
                    ))
                # There must be NO opening between dropping orphan commands and
                # stopping the customer poller. The supervisor still removes the
                # encrypted token only after its own safe teardown.
                bot.status = BotInstanceStatus.DELETING.value
                await session.flush()
                after = await count_retirement_blockers(session, project_id=project_id)
                if after.has_blockers:
                    raise ValueError(
                        "Появились новые обязательства. Экстренное отключение отменено."
                    )
                return len(actions)

    except ProjectWorkerBusy:
        raise ValueError(
            "Клиентский бот ещё обрабатывает действия доступа. "
            "Повторите отключение после остановки его воркера."
        ) from None

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
