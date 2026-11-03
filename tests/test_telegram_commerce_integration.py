import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementSource
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus
from telegram_membership_saas.modules.payments import (
    Payment,
    PaymentRequestStatus,
    create_manual_payment_method,
)
from telegram_membership_saas.modules.plans import (
    DurationUnit,
    Plan,
    PlanResource,
    PlanStatus,
)
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessResult,
    TelegramJoinTicket,
    TelegramJoinTicketStatus,
)
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.telegram_runtime import process_project_runtime_once
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for Telegram commerce integration tests",
)


def _telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_owner_and_project(session_factory) -> tuple[UUID, int, UUID]:
    owner_telegram_id = _telegram_id()
    async with session_factory() as session:
        async with session.begin():
            from telegram_membership_saas.modules.identity import TelegramUser

            owner = TelegramUser(
                telegram_user_id=owner_telegram_id,
                username=f"owner_{uuid4().hex[:8]}",
                first_name="Owner",
            )
            session.add(owner)
            await session.flush()
            owner_id = owner.id

    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name="Commerce MVP")
        session.add(project)
        await session.flush()
        return owner_id, owner_telegram_id, project.id


class FakeRuntimeAdapter:
    invite_ttl = timedelta(minutes=15)

    def __init__(self, base: datetime) -> None:
        self.base = base
        self.delivered = 0

    async def prepare(self, command):
        if command.access.action_type.value == "revoke":
            return TelegramAccessResult()
        return TelegramAccessResult(
            invite_link="https://t.me/+commerce-test",
            invite_expires_at=self.base + self.invite_ttl,
            resolved_chat_id=int(command.chat_id),
        )

    async def deliver(self, command, result):
        self.delivered += 1

    async def discard(self, result):
        return None


async def test_full_manual_checkout_review_and_access_runtime() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime.now(UTC)
    buyer_telegram_id = _telegram_id()

    try:
        owner_id, owner_telegram_id, project_id = await _create_owner_and_project(
            session_factory
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            resource = Resource(
                project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Paid channel",
            )
            plan = Plan(
                project_id=project_id,
                name="Monthly",
                duration_value=1,
                duration_unit=DurationUnit.MONTH.value,
                status=PlanStatus.ACTIVE.value,
            )
            session.add_all([resource, plan])
            await session.flush()
            session.add(
                PlanResource(
                    project_id=project_id,
                    plan_id=plan.id,
                    resource_id=resource.id,
                )
            )
            price = await replace_active_plan_price(
                session,
                project_id=project_id,
                plan_id=plan.id,
                currency_code="USD",
                amount="10",
                changed_at=base,
            )
            method = await create_manual_payment_method(
                session,
                project_id=project_id,
                name="Card USD",
                currency_codes=["USD"],
                instruction_template=(
                    "Переведите {amount} {currency} за тариф {plan} ({period})."
                ),
            )
            resource_id = resource.id
            plan_id = plan.id
            price_id = price.id
            method_id = method.id

        async with system_project_session_scope(session_factory, project_id) as session:
            options = await list_checkout_options(session, project_id=project_id)
            assert len(options) == 1
            assert options[0].plan_id == plan_id

            checkout = await create_checkout_for_telegram_user(
                session,
                project_id=project_id,
                telegram_user_id=buyer_telegram_id,
                username="buyer",
                first_name="Buyer",
                plan_price_id=price_id,
                payment_method_id=method_id,
            )
            assert checkout.request.status == PaymentRequestStatus.WAITING_CONFIRMATION.value
            assert "10 USD" in checkout.instruction
            request_id = checkout.request.id

        async with system_project_session_scope(session_factory, project_id) as session:
            submitted = await submit_checkout_for_telegram_user(
                session,
                project_id=project_id,
                request_id=request_id,
                telegram_user_id=buyer_telegram_id,
            )
            assert submitted.request.status == PaymentRequestStatus.WAITING_REVIEW.value
            assert submitted.newly_submitted is True
            assert owner_telegram_id in submitted.reviewer_telegram_ids
            assert submitted.item.customer_telegram_user_id == buyer_telegram_id

        async with system_project_session_scope(session_factory, project_id) as session:
            approved = await review_payment_request_from_telegram(
                session,
                project_id=project_id,
                request_id=request_id,
                actor_telegram_user_id=owner_telegram_id,
                actor_username="owner",
                actor_first_name="Owner",
                approve=True,
            )
            assert approved.changed is True
            assert approved.item.status == PaymentRequestStatus.APPROVED.value

        async with system_project_session_scope(session_factory, project_id) as session:
            duplicate = await review_payment_request_from_telegram(
                session,
                project_id=project_id,
                request_id=request_id,
                actor_telegram_user_id=owner_telegram_id,
                actor_username="owner",
                actor_first_name="Owner",
                approve=True,
            )
            assert duplicate.changed is False

            assert await session.scalar(
                select(Payment.id).where(Payment.payment_request_id == request_id)
            )
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.project_id == project_id,
                    Subscription.plan_id == plan_id,
                )
            )
            assert subscription is not None

            grants = tuple(
                (
                    await session.scalars(
                        select(EntitlementGrant).where(
                            EntitlementGrant.project_id == project_id,
                            EntitlementGrant.resource_id == resource_id,
                            EntitlementGrant.source == EntitlementSource.PAYMENT.value,
                        )
                    )
                ).all()
            )
            assert len(grants) == 1

            pending_outbox = tuple(
                (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.project_id == project_id,
                            OutboxEvent.status == OutboxEventStatus.PENDING.value,
                        )
                    )
                ).all()
            )
            assert pending_outbox

        adapter = FakeRuntimeAdapter(base + timedelta(seconds=5))
        did_work = await process_project_runtime_once(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            now=base + timedelta(seconds=5),
        )
        assert did_work is True
        assert adapter.delivered == 1

        async with system_project_session_scope(session_factory, project_id) as session:
            ticket = await session.scalar(
                select(TelegramJoinTicket).where(
                    TelegramJoinTicket.project_id == project_id,
                    TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
                )
            )
            assert ticket is not None
            assert ticket.telegram_user_id == buyer_telegram_id
            assert ticket.resource_id == resource_id

        async with system_project_session_scope(session_factory, project_id) as session:
            with pytest.raises(PermissionError):
                await review_payment_request_from_telegram(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    actor_telegram_user_id=buyer_telegram_id,
                    actor_username="buyer",
                    actor_first_name="Buyer",
                    approve=True,
                )
    finally:
        await engine.dispose()
