"""Read-only subscriber directory and strictly isolated manual access grants.

Zero writes to Payment, Subscription or price tables. Existing Telegram access
outbox/reconciliation remains the only writer of physical chat membership.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.access import AccessState
from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.plans import Plan, PlanResource, PlanStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.subscriptions import Subscription, SubscriptionStatus
from telegram_membership_saas.modules.subscriptions.service import calculate_period_end
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

SUBSCRIBER_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 5
FILTERS = ("all", "active", "expiring", "expired")
MANUAL_DURATIONS = (7, 30, 90, 0)


@dataclass(frozen=True, slots=True)
class SubscriberItem:
    id: UUID
    telegram_id: int
    username: str | None
    first_name: str | None
    paid: bool
    noncommercial: bool
    expiring: bool


@dataclass(frozen=True, slots=True)
class SubscriberPage:
    total: int
    page: int
    status: str
    search: str
    customers: tuple[SubscriberItem, ...]


@dataclass(frozen=True, slots=True)
class SubscriberSubscription:
    plan_name: str
    status: str
    period_end: datetime | None
    id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ActiveSubscriptionPage:
    total: int
    page: int
    subscriptions: tuple[SubscriberSubscription, ...]


@dataclass(frozen=True, slots=True)
class SubscriberPayment:
    id: UUID
    plan_name: str
    amount: str
    currency: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriberGrant:
    id: UUID
    resource_id: UUID
    resource_name: str
    source: str
    valid_until: datetime | None
    source_id: UUID | None = None
    manual_plan_id: UUID | None = None
    manual_plan_name: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriberAccess:
    resource_name: str
    desired: bool
    actual: str


@dataclass(frozen=True, slots=True)
class SubscriberInvoice:
    id: UUID
    status: str
    amount: str
    currency: str
    created_at: datetime
    plan_name: str


@dataclass(frozen=True, slots=True)
class SubscriberInvoicePage:
    customer_id: UUID
    telegram_id: int
    username: str | None
    first_name: str | None
    project_timezone: str
    total: int
    page: int
    invoices: tuple[SubscriberInvoice, ...]


@dataclass(frozen=True, slots=True)
class SubscriberDetail:
    customer_id: UUID
    telegram_id: int
    username: str | None
    first_name: str | None
    project_timezone: str
    owner_only_actions: bool
    subscriptions: tuple[SubscriberSubscription, ...]
    payments: tuple[SubscriberPayment, ...]
    total_payments: int
    history_page: int
    grants: tuple[SubscriberGrant, ...]
    total_grants: int
    grant_page: int
    access: tuple[SubscriberAccess, ...]
    resources: tuple[tuple[UUID, str], ...]
    total_invoices: int = 0
    pending_invoices: int = 0
    is_banned: bool = False


@dataclass(frozen=True, slots=True)
class ManualPlanItem:
    id: UUID
    name: str
    duration_value: int | None
    duration_unit: str
    resource_count: int


@dataclass(frozen=True, slots=True)
class ManualAccessChange:
    changed: bool
    grant_id: UUID | None
    telegram_id: int


async def _owned_customer(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
) -> tuple[ProjectCustomer, TelegramUser]:
    row = (await session.execute(
        select(ProjectCustomer, TelegramUser)
        .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
        .where(ProjectCustomer.project_id == project_id,
               ProjectCustomer.id == customer_id)
    )).one_or_none()
    if row is None:
        raise ValueError("Подписчик не найден в этом боте")
    return row


def _search_predicate(text: str):
    if not text:
        return None
    value = text.removeprefix("@").strip()
    if value.isascii() and value.isdigit():
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError("Некорректный Telegram ID") from exc
        return TelegramUser.telegram_user_id == number
    # Literal ILIKE pattern: never interpret user-supplied %, _ or backslash
    # as SQL wildcards; all values are still bound by SQLAlchemy.
    escaped = value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = "%" + escaped + "%"
    return or_(
        func.lower(TelegramUser.username).like(pattern, escape="\\"),
        func.lower(TelegramUser.first_name).like(pattern, escape="\\"),
    )


async def customer_id_by_telegram_id(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, telegram_id: int,
) -> UUID:
    """Resolve a short command only inside an authorized manager's tenant."""
    if not 0 < telegram_id <= 2**63 - 1:
        raise ValueError("Invalid Telegram ID")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        customer_id = await session.scalar(
            select(ProjectCustomer.id)
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(
                ProjectCustomer.project_id == project_id,
                TelegramUser.telegram_user_id == telegram_id,
            )
        )
        if customer_id is None:
            raise ValueError("Подписчик не найден в этом боте")
        return customer_id


async def list_subscribers(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, page: int = 0,
    status: str = "all", search: str = "",
) -> SubscriberPage:
    if status not in FILTERS or not 0 <= page <= 10000:
        raise ValueError("Недопустимый фильтр или страница")
    search = search.strip()
    if len(search) > 48 or "\n" in search or "\r" in search:
        raise ValueError("Слишком длинный поисковый запрос")
    now = datetime.now(UTC)
    soon = now + timedelta(days=7)
    active_sub = exists(select(Subscription.id).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == ProjectCustomer.id,
        Subscription.status == SubscriptionStatus.ACTIVE.value,
        or_(Subscription.current_period_end.is_(None),
            Subscription.current_period_end > now),
    ))
    expiring_sub = exists(select(Subscription.id).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == ProjectCustomer.id,
        Subscription.status == SubscriptionStatus.ACTIVE.value,
        Subscription.current_period_end > now,
        Subscription.current_period_end <= soon,
    ))
    active_grant = exists(select(EntitlementGrant.id).where(
        EntitlementGrant.project_id == project_id,
        EntitlementGrant.customer_id == ProjectCustomer.id,
        EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
        EntitlementGrant.valid_from <= now,
        or_(EntitlementGrant.valid_until.is_(None),
            EntitlementGrant.valid_until > now),
    ))
    has_history = or_(
        exists(select(Subscription.id).where(
            Subscription.project_id == project_id,
            Subscription.customer_id == ProjectCustomer.id,
        )),
        exists(select(EntitlementGrant.id).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == ProjectCustomer.id,
        )),
    )
    condition = _search_predicate(search)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        stmt = (
            select(
                ProjectCustomer.id, TelegramUser.telegram_user_id,
                TelegramUser.username, TelegramUser.first_name,
                active_sub.label("paid"), active_grant.label("noncommercial"),
                expiring_sub.label("expiring"),
            )
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(ProjectCustomer.project_id == project_id)
        )
        if condition is not None:
            stmt = stmt.where(condition)
        if status == "active":
            stmt = stmt.where(or_(active_sub, active_grant))
        elif status == "expiring":
            stmt = stmt.where(expiring_sub)
        elif status == "expired":
            stmt = stmt.where(~active_sub, ~active_grant, has_history)
        total = await session.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0
        result = (await session.execute(
            stmt.order_by(ProjectCustomer.created_at.desc(), ProjectCustomer.id.desc())
            .offset(page * SUBSCRIBER_PAGE_SIZE).limit(SUBSCRIBER_PAGE_SIZE)
        )).all()
        return SubscriberPage(
            total=total, page=page, status=status, search=search,
            customers=tuple(SubscriberItem(
                id=cid, telegram_id=tid, username=username,
                first_name=first_name, paid=paid, noncommercial=grant,
                expiring=expiring,
            ) for cid, tid, username, first_name, paid, grant, expiring in result),
        )


async def subscriber_detail(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID,
    history_page: int = 0, grant_page: int = 0,
) -> SubscriberDetail:
    if not 0 <= history_page <= 10000 or not 0 <= grant_page <= 10000:
        raise ValueError("Недопустимая страница истории")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        customer, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        subscriptions = (await session.execute(
            select(Subscription, Plan.name)
            .join(Plan, and_(Plan.id == Subscription.plan_id,
                             Plan.project_id == Subscription.project_id))
            .where(Subscription.project_id == project_id,
                   Subscription.customer_id == customer_id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .limit(15)
        )).all()
        total_invoices = await session.scalar(
            select(func.count(PaymentRequest.id)).where(
                PaymentRequest.project_id == project_id,
                PaymentRequest.customer_id == customer_id,
            )
        ) or 0
        pending_invoices = await session.scalar(
            select(func.count(PaymentRequest.id)).where(
                PaymentRequest.project_id == project_id,
                PaymentRequest.customer_id == customer_id,
                PaymentRequest.status.in_(("waiting_confirmation", "waiting_review")),
            )
        ) or 0
        total_payments = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.project_id == project_id, Payment.customer_id == customer_id,
            )
        ) or 0
        payments = (await session.execute(
            select(Payment, Plan.name)
            .join(Plan, and_(Plan.id == Payment.plan_id,
                             Plan.project_id == Payment.project_id))
            .where(Payment.project_id == project_id,
                   Payment.customer_id == customer_id)
            .order_by(Payment.approved_at.desc(), Payment.id.desc())
            .offset(history_page * HISTORY_PAGE_SIZE)
            .limit(HISTORY_PAGE_SIZE)
        )).all()
        now = datetime.now(UTC)
        grant_where = (
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.valid_from <= now,
            or_(EntitlementGrant.valid_until.is_(None),
                EntitlementGrant.valid_until > now),
        )
        total_grants = await session.scalar(
            select(func.count(EntitlementGrant.id)).where(*grant_where)
        ) or 0
        grants = (await session.execute(
            select(EntitlementGrant, Resource.name, Plan.name)
            .outerjoin(Plan, and_(Plan.id == EntitlementGrant.manual_plan_id,
                                  Plan.project_id == EntitlementGrant.project_id))
            .join(Resource, and_(
                Resource.id == EntitlementGrant.resource_id,
                Resource.project_id == EntitlementGrant.project_id,
            ))
            .where(*grant_where)
            .order_by(EntitlementGrant.created_at.desc(), EntitlementGrant.id.desc())
            .offset(grant_page * 8).limit(8)
        )).all()
        actual = (await session.execute(
            select(AccessState, Resource.name)
            .join(Resource, and_(
                Resource.id == AccessState.resource_id,
                Resource.project_id == AccessState.project_id,
            ))
            .where(AccessState.project_id == project_id,
                   AccessState.customer_id == customer_id)
            .order_by(Resource.name, Resource.id)
            .limit(30)
        )).all()
        # The owner picker paginates these resources in groups of eight.
        # Do not silently cap the entire project at 30 channels: a project
        # may have more active resources, and every one must remain reachable.
        resources = (await session.execute(
            select(Resource.id, Resource.name)
            .where(Resource.project_id == project_id,
                   Resource.status == ResourceStatus.ACTIVE.value)
            .order_by(Resource.created_at, Resource.id)
        )).all()
        return SubscriberDetail(
            customer_id=customer.id, telegram_id=user.telegram_user_id,
            username=user.username, first_name=user.first_name,
            project_timezone=project.timezone,
            owner_only_actions=project.owner_user_id == actor_id,
            subscriptions=tuple(SubscriberSubscription(
                plan_name=name, status=sub.status,
                period_end=sub.current_period_end, id=sub.id,
            ) for sub, name in subscriptions),
            payments=tuple(SubscriberPayment(
                id=payment.id, plan_name=name,
                amount=str(payment.final_amount),
                currency=payment.currency_code, approved_at=payment.approved_at,
            ) for payment, name in payments),
            total_payments=total_payments, history_page=history_page,
            total_grants=total_grants, grant_page=grant_page,
            grants=tuple(SubscriberGrant(
                id=grant.id, resource_id=grant.resource_id,
                resource_name=name, source=grant.source,
                valid_until=grant.valid_until, source_id=grant.source_id,
                manual_plan_id=grant.manual_plan_id, manual_plan_name=plan_name,
            ) for grant, name, plan_name in grants),
            access=tuple(SubscriberAccess(
                resource_name=name, desired=state.desired_access,
                actual=state.actual_state,
            ) for state, name in actual),
            resources=tuple(resources),
            total_invoices=total_invoices,
            pending_invoices=pending_invoices,
            is_banned=customer.is_banned,
        )


async def list_active_customer_subscriptions(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, page: int = 0,
) -> ActiveSubscriptionPage:
    """Paginate *active* paid plans, independent of the short customer overview."""
    if not 0 <= page <= 10000:
        raise ValueError("Недопустимая страница подписок")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Аннулирование подписок доступно только владельцу")
        await _owned_customer(session, project_id=project_id, customer_id=customer_id)
        now = datetime.now(UTC)
        predicates = (
            Subscription.project_id == project_id,
            Subscription.customer_id == customer_id,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            or_(Subscription.current_period_end.is_(None),
                Subscription.current_period_end > now),
        )
        total = await session.scalar(
            select(func.count(Subscription.id)).where(*predicates)
        ) or 0
        rows = (await session.execute(
            select(Subscription, Plan.name)
            .join(Plan, and_(
                Plan.id == Subscription.plan_id,
                Plan.project_id == Subscription.project_id,
            ))
            .where(*predicates)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return ActiveSubscriptionPage(
            total=total, page=page, subscriptions=tuple(
                SubscriberSubscription(
                    id=sub.id, plan_name=name, status=sub.status,
                    period_end=sub.current_period_end,
                ) for sub, name in rows
            ),
        )


async def get_active_customer_subscription(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, subscription_id: UUID,
) -> SubscriberSubscription:
    """Validate a chosen paid subscription by tenant-scoped ID, not a page cap."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Аннулирование подписок доступно только владельцу")
        await _owned_customer(session, project_id=project_id, customer_id=customer_id)
        now = datetime.now(UTC)
        row = (await session.execute(
            select(Subscription, Plan.name)
            .join(Plan, and_(
                Plan.id == Subscription.plan_id,
                Plan.project_id == Subscription.project_id,
            ))
            .where(
                Subscription.project_id == project_id,
                Subscription.customer_id == customer_id,
                Subscription.id == subscription_id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                or_(Subscription.current_period_end.is_(None),
                    Subscription.current_period_end > now),
            )
        )).one_or_none()
        if row is None:
            raise ValueError("Активная подписка этого покупателя не найдена")
        sub, name = row
        return SubscriberSubscription(
            id=sub.id, plan_name=name, status=sub.status,
            period_end=sub.current_period_end,
        )


async def list_subscriber_invoices(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, page: int = 0,
) -> SubscriberInvoicePage:
    """Read-only project-scoped invoice history for an existing buyer."""
    if not 0 <= page <= 10000:
        raise ValueError("Недопустимая страница счетов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        customer, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        condition = (
            PaymentRequest.project_id == project_id,
            PaymentRequest.customer_id == customer.id,
        )
        total = await session.scalar(
            select(func.count(PaymentRequest.id)).where(*condition)
        ) or 0
        rows = (await session.execute(
            select(PaymentRequest, Plan.name)
            .join(Plan, and_(
                Plan.id == PaymentRequest.plan_id,
                Plan.project_id == PaymentRequest.project_id,
            ))
            .where(*condition)
            .order_by(PaymentRequest.created_at.desc(), PaymentRequest.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return SubscriberInvoicePage(
            customer_id=customer.id, telegram_id=user.telegram_user_id,
            username=user.username, first_name=user.first_name,
            project_timezone=project.timezone, total=total, page=page,
            invoices=tuple(SubscriberInvoice(
                id=request.id, status=request.status,
                amount=str(request.final_amount), currency=request.currency_code,
                created_at=request.created_at, plan_name=plan_name,
            ) for request, plan_name in rows),
        )


async def grant_manual_access(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID,
    resource_id: UUID, days: int,
) -> ManualAccessChange:
    if days not in MANUAL_DURATIONS:
        raise ValueError("Недопустимый срок ручного доступа")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Выдать ручной доступ может только владелец")
        # A removal transaction may have committed immediately before this
        # grant obtains the same advisory lock. Never resurrect a retiring
        # bot by creating new entitlements after status=deleting.
        removing = await session.scalar(select(BotInstance.id).where(
            BotInstance.project_id == project_id,
            BotInstance.status == BotInstanceStatus.DELETING.value,
        ).limit(1))
        if removing is not None:
            raise ValueError("Бот отключается. Выдача новых прав временно недоступна")
        customer, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        if customer.user_id == actor_id:
            raise ValueError("Владелец уже получает отдельный служебный доступ")
        resource = await session.scalar(select(Resource).where(
            Resource.project_id == project_id, Resource.id == resource_id,
            Resource.status == ResourceStatus.ACTIVE.value,
        ))
        if resource is None:
            raise ValueError("Канал отключён или не найден")
        now = datetime.now(UTC)
        existing = await session.scalar(select(EntitlementGrant.id).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.resource_id == resource_id,
            EntitlementGrant.source == EntitlementSource.MANUAL.value,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.valid_from <= now,
            or_(EntitlementGrant.valid_until.is_(None),
                EntitlementGrant.valid_until > now),
        ).limit(1))
        if existing:
            raise ValueError(
                "Ручной доступ уже действует. Сначала отзовите его, если нужно изменить срок."
            )
        grant = EntitlementGrant(
            project_id=project_id, customer_id=customer_id,
            resource_id=resource_id, source=EntitlementSource.MANUAL.value,
            source_id=uuid4(), valid_from=now,
            valid_until=now + timedelta(days=days) if days else None,
            status=EntitlementGrantStatus.ACTIVE.value,
        )
        session.add(grant)
        await session.flush()
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=customer_id, resource_id=resource_id,
        )
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="manual_grant", target_id=grant.id,
            reason=f"Ручной доступ: {'бессрочно' if days == 0 else str(days) + ' дней'}",
        ))
        return ManualAccessChange(
            changed=True, grant_id=grant.id, telegram_id=user.telegram_user_id,
        )


async def revoke_manual_access(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, grant_id: UUID,
) -> ManualAccessChange:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Отозвать ручной доступ может только владелец")
        _, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        grant = await session.scalar(select(EntitlementGrant).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.id == grant_id,
            EntitlementGrant.source == EntitlementSource.MANUAL.value,
        ).with_for_update())
        if grant is None:
            raise ValueError("Ручное право этого подписчика не найдено")
        if grant.status == EntitlementGrantStatus.REVOKED.value:
            return ManualAccessChange(
                changed=False, grant_id=grant.id,
                telegram_id=user.telegram_user_id,
            )
        grant.status = EntitlementGrantStatus.REVOKED.value
        grant.revoked_at = max(datetime.now(UTC), grant.valid_from)
        await session.flush()
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=customer_id, resource_id=grant.resource_id,
        )
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="manual_revoke", target_id=grant.id,
            reason="Владелец отозвал только отдельное ручное право доступа",
        ))
        return ManualAccessChange(
            changed=True, grant_id=grant.id,
            telegram_id=user.telegram_user_id,
        )


async def set_customer_ban(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, banned: bool,
) -> bool:
    """Owner-only per-project ban: no deletion or paid entitlement revocation."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Блокировать покупателей может только владелец")
        customer, _ = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        if customer.user_id == actor_id:
            raise ValueError("Нельзя заблокировать владельца")
        await session.refresh(customer, with_for_update=True)
        if customer.is_banned == banned:
            return False
        customer.is_banned = banned
        customer.banned_at = datetime.now(UTC) if banned else None
        customer.banned_by_user_id = actor_id if banned else None
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="customer_ban" if banned else "customer_unban",
            target_id=customer.id,
            reason="Запрет новых счетов и отправки чеков; оплаченный доступ сохранён",
        ))
        return True


async def list_activatable_plans(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, page: int = 0,
) -> tuple[ManualPlanItem, ...]:
    if not 0 <= page <= 100:
        raise ValueError("Недопустимая страница тарифов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Выдавать бесплатные тарифы может только владелец")
        await _owned_customer(session, project_id=project_id, customer_id=customer_id)
        rows = (await session.execute(
            select(Plan.id, Plan.name, Plan.duration_value, Plan.duration_unit,
                   func.count(Resource.id))
            .join(PlanResource, and_(
                PlanResource.plan_id == Plan.id,
                PlanResource.project_id == Plan.project_id,
            ))
            .join(Resource, and_(
                Resource.id == PlanResource.resource_id,
                Resource.project_id == PlanResource.project_id,
            ))
            .where(
                Plan.project_id == project_id,
                Plan.status == PlanStatus.ACTIVE.value,
                Resource.status == ResourceStatus.ACTIVE.value,
            )
            .group_by(Plan.id)
            .order_by(Plan.sort_order, Plan.created_at, Plan.id)
            .offset(page * 8).limit(8)
        )).all()
        return tuple(ManualPlanItem(
            id=pid, name=name, duration_value=value,
            duration_unit=unit, resource_count=count,
        ) for pid, name, value, unit, count in rows)


async def activate_manual_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, plan_id: UUID,
) -> ManualAccessChange:
    """Grant all plan resources atomically without inventing a payment."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Активировать тариф может только владелец")
        if await session.scalar(select(BotInstance.id).where(
            BotInstance.project_id == project_id,
            BotInstance.status == BotInstanceStatus.DELETING.value,
        ).limit(1)) is not None:
            raise ValueError("Бот отключается. Выдача новых прав недоступна")
        customer, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        if customer.user_id == actor_id:
            raise ValueError("Владельцу уже выдан служебный доступ")
        if customer.is_banned:
            raise ValueError("Сначала снимите блокировку покупателя")
        plan = await session.scalar(
            select(Plan).where(
                Plan.project_id == project_id, Plan.id == plan_id,
                Plan.status == PlanStatus.ACTIVE.value,
            )
        )
        if plan is None:
            raise ValueError("Тариф недоступен")
        targets = (await session.scalars(
            select(Resource.id)
            .join(PlanResource, and_(
                PlanResource.resource_id == Resource.id,
                PlanResource.project_id == Resource.project_id,
            ))
            .where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id == plan_id,
                Resource.status == ResourceStatus.ACTIVE.value,
            ).order_by(Resource.id)
        )).all()
        if not targets:
            raise ValueError("У тарифа нет активных каналов")
        now = datetime.now(UTC)
        active = await session.scalar(
            select(EntitlementGrant.id).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == customer_id,
                EntitlementGrant.manual_plan_id == plan_id,
                EntitlementGrant.source == EntitlementSource.MANUAL.value,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                EntitlementGrant.valid_from <= now,
                or_(EntitlementGrant.valid_until.is_(None),
                    EntitlementGrant.valid_until > now),
            ).limit(1)
        )
        if active:
            raise ValueError("Этот тариф уже выдан бесплатно — сначала аннулируйте его")
        expiry = calculate_period_end(
            now, timezone_name=project.timezone,
            duration_value=plan.duration_value, duration_unit=plan.duration_unit,
        )
        activation_id = uuid4()
        for target in targets:
            session.add(EntitlementGrant(
                project_id=project_id, customer_id=customer_id,
                resource_id=target, source=EntitlementSource.MANUAL.value,
                source_id=activation_id, manual_plan_id=plan_id,
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=now, valid_until=expiry,
            ))
        await session.flush()
        for target in targets:
            await reconcile_access_target(
                session, project_id=project_id, customer_id=customer_id,
                resource_id=target,
            )
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="manual_plan_activate", target_id=activation_id,
            reason="Бесплатная выдача тарифа без записи об оплате: " + plan.name[:110],
        ))
        return ManualAccessChange(
            changed=True, grant_id=activation_id,
            telegram_id=user.telegram_user_id,
        )


async def revoke_manual_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID,
    activation_id: UUID,
) -> ManualAccessChange:
    """Undo only one complimentary plan activation, never paid grants."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        if project.owner_user_id != actor_id:
            raise PermissionError("Аннулировать доступ может только владелец")
        _, user = await _owned_customer(
            session, project_id=project_id, customer_id=customer_id,
        )
        grants = (await session.scalars(
            select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == customer_id,
                EntitlementGrant.source_id == activation_id,
                EntitlementGrant.source == EntitlementSource.MANUAL.value,
                EntitlementGrant.manual_plan_id.is_not(None),
            ).with_for_update()
        )).all()
        if not grants:
            raise ValueError("Ручная активация не найдена")
        now = datetime.now(UTC)
        targets = set()
        for grant in grants:
            if grant.status != EntitlementGrantStatus.ACTIVE.value:
                continue
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = max(now, grant.valid_from)
            targets.add(grant.resource_id)
        if not targets:
            return ManualAccessChange(
                changed=False, grant_id=activation_id,
                telegram_id=user.telegram_user_id,
            )
        await session.flush()
        for target in sorted(targets):
            await reconcile_access_target(
                session, project_id=project_id,
                customer_id=customer_id, resource_id=target,
            )
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="manual_plan_revoke", target_id=activation_id,
            reason="Отозвана только бесплатная активация тарифа; платежи сохранены",
        ))
        return ManualAccessChange(
            changed=True, grant_id=activation_id,
            telegram_id=user.telegram_user_id,
        )
