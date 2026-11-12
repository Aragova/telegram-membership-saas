"""Owner-only remediation of obligations that block BotInstance retirement.

No financial row is deleted and no access is considered revoked until the
existing Telegram access worker confirms the physical state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.access import AccessState
from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.bots.deletion_service import (
    RetirementBlockers,
    count_retirement_blockers,
    owner_self_managed_access_predicate,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.payments import (
    Payment,
    PaymentRequest,
    PaymentRequestStatus,
    cancel_payment_request,
    reject_payment_request,
)
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.stars_subscriptions import SubscriptionStarOrder
from telegram_membership_saas.modules.subscriptions import Subscription, SubscriptionStatus
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

OPEN_STATUSES = (
    PaymentRequestStatus.WAITING_CONFIRMATION.value,
    PaymentRequestStatus.WAITING_REVIEW.value,
)


@dataclass(frozen=True, slots=True)
class OwnerPayment:
    id: UUID
    telegram_user_id: int
    username: str | None
    status: str
    amount: str
    currency: str


@dataclass(frozen=True, slots=True)
class OwnerSubscription:
    id: UUID
    telegram_user_id: int
    username: str | None
    plan_name: str
    period_end: datetime | None


@dataclass(frozen=True, slots=True)
class OwnerGrant:
    id: UUID
    customer_id: UUID
    telegram_user_id: int
    source: str
    resource_id: UUID


@dataclass(frozen=True, slots=True)
class OwnerAccess:
    id: UUID
    customer_id: UUID
    resource_id: UUID
    desired: bool
    actual: str


@dataclass(frozen=True, slots=True)
class OwnerObligations:
    blockers: RetirementBlockers
    payments: tuple[OwnerPayment, ...]
    subscriptions: tuple[OwnerSubscription, ...]
    grants: tuple[OwnerGrant, ...]
    access: tuple[OwnerAccess, ...]


@dataclass(frozen=True, slots=True)
class OwnerChange:
    changed: bool
    telegram_user_id: int | None
    message: str


async def _require_owner(session: AsyncSession, *, project_id: UUID, actor_id: UUID) -> None:
    owner = await session.scalar(select(Project.owner_user_id).where(Project.id == project_id))
    if owner != actor_id:
        raise PermissionError("Действие доступно только владельцу проекта")


def _active_at(grant: EntitlementGrant, moment: datetime) -> bool:
    return (grant.status == EntitlementGrantStatus.ACTIVE.value
            and (grant.valid_until is None or grant.valid_until > moment))


async def _telegram_id(session: AsyncSession, project_id: UUID, customer_id: UUID) -> int | None:
    return await session.scalar(
        select(TelegramUser.telegram_user_id)
        .join(ProjectCustomer, ProjectCustomer.user_id == TelegramUser.id)
        .where(ProjectCustomer.project_id == project_id, ProjectCustomer.id == customer_id)
    )


def _audit(
    session: AsyncSession, project_id: UUID, actor_id: UUID,
    action: str, target_id: UUID, reason: str,
) -> None:
    session.add(OwnerObligationAudit(
        project_id=project_id, actor_id=actor_id, action=action,
        target_id=target_id, reason=reason,
    ))


async def list_owner_obligations(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, page: int = 0,
) -> OwnerObligations:
    if page < 0 or page > 200:
        raise ValueError("Недопустимая страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        blockers = await count_retirement_blockers(session, project_id=project_id)
        now = datetime.now(UTC)
        payments = (await session.execute(
            select(PaymentRequest, TelegramUser.telegram_user_id, TelegramUser.username)
            .join(ProjectCustomer, and_(
                ProjectCustomer.id == PaymentRequest.customer_id,
                ProjectCustomer.project_id == PaymentRequest.project_id,
            ))
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(PaymentRequest.project_id == project_id,
                   PaymentRequest.status.in_(OPEN_STATUSES))
            .order_by(PaymentRequest.created_at, PaymentRequest.id).offset(page * 5).limit(5)
        )).all()
        subscriptions = (await session.execute(
            select(Subscription, Plan.name, TelegramUser.telegram_user_id, TelegramUser.username)
            .join(Plan, and_(Plan.id == Subscription.plan_id,
                             Plan.project_id == Subscription.project_id))
            .join(ProjectCustomer, and_(
                ProjectCustomer.id == Subscription.customer_id,
                ProjectCustomer.project_id == Subscription.project_id,
            ))
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(Subscription.project_id == project_id,
                   Subscription.status == SubscriptionStatus.ACTIVE.value,
                   or_(Subscription.current_period_end.is_(None),
                       Subscription.current_period_end > now))
            .order_by(Subscription.created_at, Subscription.id).offset(page * 5).limit(5)
        )).all()
        grants = (await session.execute(
            select(EntitlementGrant, TelegramUser.telegram_user_id)
            .join(ProjectCustomer, and_(
                ProjectCustomer.id == EntitlementGrant.customer_id,
                ProjectCustomer.project_id == EntitlementGrant.project_id,
            ))
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(EntitlementGrant.project_id == project_id,
                   EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                   or_(EntitlementGrant.valid_until.is_(None),
                       EntitlementGrant.valid_until > now))
            .order_by(EntitlementGrant.created_at, EntitlementGrant.id).offset(page * 5).limit(5)
        )).all()
        access = (await session.scalars(
            select(AccessState)
            .where(AccessState.project_id == project_id,
                   or_(AccessState.desired_access.is_(True),
                       AccessState.actual_state == "granted"),
                   ~owner_self_managed_access_predicate())
            .order_by(AccessState.created_at, AccessState.id).offset(page * 5).limit(5)
        )).all()
        return OwnerObligations(
            blockers=blockers,
            payments=tuple(OwnerPayment(
                id=req.id, telegram_user_id=tid, username=username,
                status=req.status, amount=str(req.final_amount), currency=req.currency_code,
            ) for req, tid, username in payments),
            subscriptions=tuple(OwnerSubscription(
                id=sub.id, telegram_user_id=tid, username=username,
                plan_name=name, period_end=sub.current_period_end,
            ) for sub, name, tid, username in subscriptions),
            grants=tuple(OwnerGrant(
                id=g.id, customer_id=g.customer_id, telegram_user_id=tid,
                source=g.source, resource_id=g.resource_id,
            ) for g, tid in grants),
            access=tuple(OwnerAccess(
                id=s.id, customer_id=s.customer_id, resource_id=s.resource_id,
                desired=s.desired_access, actual=s.actual_state,
            ) for s in access),
        )


async def close_owner_payment(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, request_id: UUID,
) -> OwnerChange:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        request = await session.scalar(
            select(PaymentRequest)
            .where(PaymentRequest.project_id == project_id, PaymentRequest.id == request_id)
            .with_for_update()
        )
        if request is None:
            raise ValueError("Заявка не найдена в этом проекте")
        if request.status in (
            PaymentRequestStatus.CANCELLED.value, PaymentRequestStatus.REJECTED.value,
        ):
            return OwnerChange(False, None, "Заявка уже закрыта")
        if request.status == PaymentRequestStatus.WAITING_CONFIRMATION.value:
            await cancel_payment_request(
                session, project_id=project_id, request_id=request_id,
                customer_id=request.customer_id,
            )
            action = "payment_cancel"
            message = (
                "Заявка на оплату закрыта владельцем. "
                "Если деньги были переведены, свяжитесь с владельцем канала."
            )
        elif request.status == PaymentRequestStatus.WAITING_REVIEW.value:
            await reject_payment_request(
                session, project_id=project_id, request_id=request_id,
                reviewer_user_id=actor_id,
            )
            action = "payment_reject"
            message = (
                "Владелец отклонил заявку. Если вы уже перевели деньги, "
                "свяжитесь с владельцем канала."
            )
        else:
            raise ValueError("Заявку уже обработали. Обновите список.")
        _audit(session, project_id, actor_id, action, request_id,
               "Владелец подтвердил завершение заявки перед отключением бота")
        return OwnerChange(
            True, await _telegram_id(session, project_id, request.customer_id),
            message,
        )


async def end_owner_subscription(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, subscription_id: UUID,
) -> OwnerChange:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        sub = await session.scalar(
            select(Subscription).where(
                Subscription.project_id == project_id, Subscription.id == subscription_id
            ).with_for_update()
        )
        if sub is None:
            raise ValueError("Подписка не найдена в этом проекте")
        if sub.status == SubscriptionStatus.CANCELLED.value:
            return OwnerChange(False, None, "Подписка уже завершена")
        if sub.status != SubscriptionStatus.ACTIVE.value:
            raise ValueError("Подписка уже неактивна")
        sub.status = SubscriptionStatus.CANCELLED.value
        # A subscription can have multiple paid periods. Payment grants are
        # tied to Payment.id, never to Subscription.id; revoke all of them
        # for this exact customer+plan while preserving other entitlement sources.
        grants = (await session.scalars(
            select(EntitlementGrant)
            .join(Payment, and_(
                EntitlementGrant.source_id == Payment.id,
                EntitlementGrant.project_id == Payment.project_id,
            ))
            .where(EntitlementGrant.project_id == project_id,
                   EntitlementGrant.customer_id == sub.customer_id,
                   EntitlementGrant.source == EntitlementSource.PAYMENT.value,
                   EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                   Payment.plan_id == sub.plan_id,
                   Payment.customer_id == sub.customer_id)
            .with_for_update()
        )).all()
        # Native Stars purchases belong to the SAME customer and plan.
        # Revoking a subscription must not leave lifetime Stars grants live.
        star_grants = (await session.scalars(
            select(EntitlementGrant)
            .join(SubscriptionStarOrder, and_(
                EntitlementGrant.source_id == SubscriptionStarOrder.id,
                EntitlementGrant.project_id == SubscriptionStarOrder.project_id,
            ))
            .where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == sub.customer_id,
                EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                SubscriptionStarOrder.customer_id == sub.customer_id,
                SubscriptionStarOrder.plan_id == sub.plan_id,
            )
            .with_for_update()
        )).all()
        crypto_grants = (await session.scalars(
            select(EntitlementGrant)
            .join(CryptoPayInvoice, and_(
                EntitlementGrant.source_id == CryptoPayInvoice.id,
                EntitlementGrant.project_id == CryptoPayInvoice.project_id,
            ))
            .where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == sub.customer_id,
                EntitlementGrant.source == EntitlementSource.CRYPTO_PAYMENT.value,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                CryptoPayInvoice.customer_id == sub.customer_id,
                CryptoPayInvoice.plan_id == sub.plan_id,
                CryptoPayInvoice.kind == "subscription",
                CryptoPayInvoice.status == "paid",
            ).with_for_update()
        )).all()
        now = datetime.now(UTC)
        targets: set[UUID] = set()
        for grant in (*grants, *star_grants, *crypto_grants):
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = max(now, grant.valid_from)
            targets.add(grant.resource_id)
        await session.flush()
        for resource_id in sorted(targets):
            await reconcile_access_target(
                session, project_id=project_id,
                customer_id=sub.customer_id, resource_id=resource_id,
            )
        _audit(session, project_id, actor_id, "subscription_end", subscription_id,
               "Владелец подтвердил досрочное прекращение; "
               "возврат денег урегулируется вне ViMi Pay")
        return OwnerChange(
            True, await _telegram_id(session, project_id, sub.customer_id),
            (
                "Владелец завершил подписку. Оплаченный срок мог не закончиться; "
                "по вопросам возврата свяжитесь с владельцем."
            ),
        )


async def revoke_owner_grant(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, grant_id: UUID,
) -> OwnerChange:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        grant = await session.scalar(select(EntitlementGrant).where(
            EntitlementGrant.project_id == project_id, EntitlementGrant.id == grant_id
        ).with_for_update())
        if grant is None:
            raise ValueError("Право доступа не найдено")
        if grant.status == EntitlementGrantStatus.REVOKED.value:
            return OwnerChange(False, None, "Право уже отозвано")
        if grant.source in (
            EntitlementSource.PAYMENT.value,
            EntitlementSource.STARS_PAYMENT.value,
            EntitlementSource.CRYPTO_PAYMENT.value,
        ):
            raise ValueError("Оплаченный доступ закрывается через завершение подписки")
        grant.status = EntitlementGrantStatus.REVOKED.value
        grant.revoked_at = max(datetime.now(UTC), grant.valid_from)
        await session.flush()
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=grant.customer_id, resource_id=grant.resource_id,
        )
        _audit(session, project_id, actor_id, "grant_revoke", grant_id,
               "Владелец отдельно подтвердил отзыв некоммерческого права доступа")
        return OwnerChange(
            True, await _telegram_id(session, project_id, grant.customer_id),
            (
                "Владелец отозвал доступ к ресурсу. "
                "Если остались другие действующие права, доступ сохраняется."
            ),
        )


async def retry_owner_access(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, state_id: UUID,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        state = await session.scalar(select(AccessState).where(
            AccessState.project_id == project_id,
            AccessState.id == state_id,
        ))
        if state is None:
            raise ValueError("Доступ не найден в этом проекте")
        result = await reconcile_access_target(
            session, project_id=project_id,
            customer_id=state.customer_id, resource_id=state.resource_id,
        )
        if result.effective_access.has_access:
            return "Доступ сохраняется по другому действующему праву"
        return ("Отзыв поставлен в очередь Telegram"
                if result.queued_action else
                "Состояние синхронизировано; проверьте фактический доступ")
