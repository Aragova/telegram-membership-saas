"""Real PostgreSQL RLS: subscribers and manual grants preserve paid rights."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.access import EffectiveAccess
from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.owner_setup.subscribers import (
    customer_id_by_telegram_id,
    grant_manual_access,
    list_subscribers,
    revoke_manual_access,
    subscriber_detail,
)
from telegram_membership_saas.modules.payments import Payment
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


def tg_id() -> int:
    return (uuid4().int % 7_000_000_000_000_000_000) + 1


async def setup_subscriber(factory):
    owner_tid, buyer_tid, outside_tid = tg_id(), tg_id(), tg_id()
    owner = await ensure_telegram_actor(
        factory, telegram_id=owner_tid, username="sub_owner",
        first_name="Owner",
    )
    buyer = await ensure_telegram_actor(
        factory, telegram_id=buyer_tid, username="subscriber_beta",
        first_name="Buyer",
    )
    outsider = await ensure_telegram_actor(
        factory, telegram_id=outside_tid, username="sub_outside",
        first_name="Outsider",
    )
    project = await create_owner_project(
        factory, actor_id=owner, name="Subscriber-test project",
    )
    other_project = await create_owner_project(
        factory, actor_id=outsider, name="Foreign project",
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        customer = ProjectCustomer(project_id=project, user_id=buyer)
        channel = Resource(
            project_id=project, resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=f"-100{uuid4().int % 100000000000}",
            name="Private Channel", status=ResourceStatus.ACTIVE.value,
        )
        session.add_all([customer, channel])
        await session.flush()
        cid, rid = customer.id, channel.id
    return owner, buyer, outsider, project, other_project, cid, rid, buyer_tid


async def test_short_code_customer_lookup_requires_correct_tenant_and_manager():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, _, buyer_tid = (
            await setup_subscriber(factory)
        )
        assert await customer_id_by_telegram_id(
            factory, actor_id=owner, project_id=project,
            telegram_id=buyer_tid,
        ) == cid
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await customer_id_by_telegram_id(
                factory, actor_id=outsider, project_id=project,
                telegram_id=buyer_tid,
            )
        with pytest.raises(ValueError, match="Подписчик"):
            await customer_id_by_telegram_id(
                factory, actor_id=outsider, project_id=other,
                telegram_id=buyer_tid,
            )
        with pytest.raises(ValueError, match="Подписчик"):
            await customer_id_by_telegram_id(
                factory, actor_id=owner, project_id=project,
                telegram_id=tg_id(),
            )
    finally:
        await engine.dispose()


async def test_manual_grant_and_revoke_isolated_from_other_projects():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, rid, buyer_tid = (
            await setup_subscriber(factory)
        )
        page = await list_subscribers(
            factory, actor_id=owner, project_id=project,
        )
        assert page.total >= 1
        assert any(x.id == cid for x in page.customers)
        match = await list_subscribers(
            factory, actor_id=owner, project_id=project,
            search="subscriber_beta",
        )
        assert any(x.id == cid for x in match.customers)
        assert (await list_subscribers(
            factory, actor_id=owner, project_id=project, search=str(buyer_tid),
        )).total == 1
        assert (await list_subscribers(
            factory, actor_id=owner, project_id=project, status="active",
            search=str(buyer_tid),
        )).total == 0
        assert (await list_subscribers(
            factory, actor_id=owner, project_id=project,
            search="no_such_telegram_user",
        )).total == 0
        for call, args in (
            (list_subscribers, {"search": str(buyer_tid)}),
            (subscriber_detail, {"customer_id": cid}),
            (grant_manual_access, {"customer_id": cid, "resource_id": rid, "days": 7}),
        ):
            with pytest.raises((PermissionError, TenantAccessDenied)):
                await call(
                    factory, actor_id=outsider, project_id=project, **args,
                )
        for days in (-1, 1, 100000):
            with pytest.raises(ValueError, match="срок"):
                await grant_manual_access(
                    factory, actor_id=owner, project_id=project,
                    customer_id=cid, resource_id=rid, days=days,
                )
        with pytest.raises(ValueError, match="Канал"):
            await grant_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, resource_id=uuid4(), days=7,
            )
        created = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, resource_id=rid, days=7,
        )
        assert created.changed and created.telegram_id == buyer_tid
        with pytest.raises(ValueError, match="уже действует"):
            await grant_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, resource_id=rid, days=7,
            )
        card = await subscriber_detail(
            factory, actor_id=owner, project_id=project, customer_id=cid,
        )
        assert card.telegram_id == buyer_tid and card.owner_only_actions
        assert len(card.grants) == 1
        assert card.grants[0].source == EntitlementSource.MANUAL.value
        assert card.grants[0].valid_until is not None
        assert card.grants[0].resource_name == "Private Channel"
        assert (await list_subscribers(
            factory, actor_id=owner, project_id=project,
            status="active", search=str(buyer_tid),
        )).total == 1

        revoked = await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_id=created.grant_id,
        )
        assert revoked.changed
        assert not (await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_id=created.grant_id,
        )).changed
        assert (await list_subscribers(
            factory, actor_id=owner, project_id=project,
            status="expired", search=str(buyer_tid),
        )).total == 1
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            projection = await session.scalar(select(EffectiveAccess).where(
                EffectiveAccess.project_id == project,
                EffectiveAccess.customer_id == cid,
                EffectiveAccess.resource_id == rid,
            ))
            assert projection is not None and not projection.has_access
            decisions = (await session.scalars(
                select(OwnerObligationAudit.action).where(
                    OwnerObligationAudit.project_id == project,
                    OwnerObligationAudit.target_id == created.grant_id,
                ).order_by(OwnerObligationAudit.created_at)
            )).all()
            assert decisions == ["manual_grant", "manual_revoke"]
            assert await session.scalar(select(func.count()).select_from(
                Payment,
            ).where(Payment.project_id == project)) == 0
            assert await session.scalar(select(func.count()).select_from(
                Subscription,
            ).where(Subscription.project_id == project)) == 0
        with pytest.raises(ValueError, match="не найден"):
            await subscriber_detail(
                factory, actor_id=owner, project_id=project,
                customer_id=uuid4(),
            )
        with pytest.raises(ValueError, match="не найден"):
            await revoke_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=uuid4(), grant_id=created.grant_id,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await list_subscribers(
                factory, actor_id=owner, project_id=other,
            )
    finally:
        await engine.dispose()


async def test_revoke_manual_leaves_existing_paid_entitlement_active():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, cid, rid, _ = await setup_subscriber(factory)
        now = datetime.now(UTC)
        paid_id = uuid4()
        # Existing paid grant, as produced by the real atomic approval
        # pipeline. This feature must never revoke or extend it.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(EntitlementGrant(
                id=paid_id, project_id=project, customer_id=cid,
                resource_id=rid, source=EntitlementSource.PAYMENT.value,
                source_id=uuid4(), valid_from=now,
                valid_until=now + timedelta(days=40),
                status=EntitlementGrantStatus.ACTIVE.value,
            ))
            await session.flush()
            await reconcile_access_target(
                session, project_id=project,
                customer_id=cid, resource_id=rid,
            )
        manual = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, resource_id=rid, days=0,
        )
        await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_id=manual.grant_id,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            paid = await session.get(EntitlementGrant, paid_id)
            assert paid.status == EntitlementGrantStatus.ACTIVE.value
            assert paid.revoked_at is None
            projection = await session.scalar(select(EffectiveAccess).where(
                EffectiveAccess.project_id == project,
                EffectiveAccess.customer_id == cid,
                EffectiveAccess.resource_id == rid,
            ))
            assert projection.has_access and projection.active_grant_count == 1
        with pytest.raises(ValueError, match="не найден"):
            await revoke_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, grant_id=paid_id,
            )
    finally:
        await engine.dispose()



async def test_retiring_bot_blocks_grants_and_older_grants_are_paginated():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, cid, rid, _ = await setup_subscriber(factory)
        now = datetime.now(UTC)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            # Existing rights may be much older than the latest 30 payments;
            # the owner's UI must still find and revoke them.
            session.add_all([
                EntitlementGrant(
                    project_id=project, customer_id=cid,
                    resource_id=rid, source=EntitlementSource.MANUAL.value,
                    source_id=uuid4(), valid_from=now,
                    valid_until=now + timedelta(days=20),
                    status=EntitlementGrantStatus.ACTIVE.value,
                )
                for _ in range(34)
            ])
        first = await subscriber_detail(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_page=0,
        )
        last = await subscriber_detail(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_page=4,
        )
        assert first.total_grants == last.total_grants == 34
        assert len(first.grants) == 8 and len(last.grants) == 2
        assert {g.id for g in first.grants}.isdisjoint({g.id for g in last.grants})
        target = last.grants[0].id
        await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, grant_id=target,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=tg_id(),
                username="retiring_customer_bot", display_name="Retiring",
                token_ciphertext="test-only-no-real-token",
                status="deleting",
            ))
        with pytest.raises(ValueError, match="отключается"):
            await grant_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, resource_id=rid, days=7,
            )
        # The transaction must not create another noncommercial entitlement.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project,
                EntitlementGrant.customer_id == cid,
                EntitlementGrant.source == EntitlementSource.MANUAL.value,
            )) == 34
    finally:
        await engine.dispose()


async def test_owner_resource_picker_does_not_truncate_after_thirty_channels():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, cid, initial, _ = await setup_subscriber(factory)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            for n in range(35):
                session.add(Resource(
                    project_id=project,
                    resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                    external_id=f"-100{uuid4().int % 100000000000}",
                    name=f"Extra private channel {n:02d}",
                    status=ResourceStatus.ACTIVE.value,
                ))
        detail = await subscriber_detail(
            factory, actor_id=owner, project_id=project,
            customer_id=cid,
        )
        assert len(detail.resources) == 36
        assert initial in {resource_id for resource_id, _ in detail.resources}
        assert "Extra private channel 34" in {
            name for _, name in detail.resources
        }
        assert len(detail.resources[32:40]) == 4
    finally:
        await engine.dispose()
