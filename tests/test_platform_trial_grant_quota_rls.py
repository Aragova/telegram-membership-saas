"""PostgreSQL integration checks for inactive-by-default trial and shared free-grant limit."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import VerifiedBot
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementSource
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.owner_setup import transfers as transfer_service
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.owner_setup.subscribers import (
    activate_manual_plan,
    grant_manual_access,
    revoke_manual_access,
)
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanResource, PlanStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires a migrated PostgreSQL database with privileged test role",
)


async def _set_policy(admin_engine, *, active, effective_at=None):
    async with admin_engine.begin() as admin:
        await admin.execute(text(
            "UPDATE platform_saas_access_policy "
            "SET enabled=:active,effective_from=:effective WHERE id=1"
        ), {
            "active": active,
            "effective": (
                effective_at or datetime.now(UTC) - timedelta(hours=1)
            ) if active else None,
        })


async def _connect(
    factory, actor, project, *, status="registered", backdated=False,
):
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor, project_id=project),
    ) as session:
        bot = BotInstance(
            project_id=project, telegram_bot_id=tg_id(),
            username="trial_test_bot", display_name="Trial test bot",
            token_ciphertext="placeholder-do-not-use", status=status,
        )
        if backdated:
            bot.created_at = datetime.now(UTC) - timedelta(days=30)
        session.add(bot)
        await session.flush()
        return bot.id


async def _count_claims(admin_engine, owner):
    async with admin_engine.connect() as admin:
        return (await admin.execute(text("""
            SELECT count(*)
            FROM merchant_platform_trial_claims
            WHERE owner_user_id=:owner AND policy_key='vimi-saas-72h-v1'
              AND scope='owner'
        """), {"owner": owner})).scalar_one()


async def test_trial_is_opt_in_once_per_owner_and_first_bot_only():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner = await ensure_telegram_actor(
        factory, telegram_id=tg_id(), username=None, first_name=None,
    )
    old_owner = await ensure_telegram_actor(
        factory, telegram_id=tg_id(), username=None, first_name=None,
    )
    project_a = await create_owner_project(
        factory, actor_id=owner, name="Trial project A",
    )
    project_b = await create_owner_project(
        factory, actor_id=owner, name="Trial project B",
    )
    grandfathered = await create_owner_project(
        factory, actor_id=old_owner, name="Existing merchant",
    )
    try:
        # With policy OFF, connecting does not claim trials or restrict users.
        await _connect(factory, old_owner, grandfathered)
        assert await _count_claims(admin_engine, old_owner) == 0
        # The rollout instant must be AFTER the existing bot's connection,
        # otherwise that connection would correctly count as a new owner.
        await _set_policy(
            admin_engine, active=True, effective_at=datetime.now(UTC),
        )
        first_bot = await _connect(factory, owner, project_a)
        assert await _count_claims(admin_engine, owner) == 1
        # Same account, another project & Merchant ID: no second free bot.
        with pytest.raises(DBAPIError) as error:
            await _connect(factory, owner, project_b)
        assert getattr(error.value.orig, "sqlstate", None) == "P6703"
        assert await _count_claims(admin_engine, owner) == 1
        # Marking bot A as 'deleting' must NOT release the trial slot
        # before the actual row is removed. Otherwise A can be restored
        # to 'registered' after adding bot B in another project.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            item = await session.get(BotInstance, first_bot)
            item.status = "deleting"
        with pytest.raises(DBAPIError) as error:
            await _connect(factory, owner, project_b)
        assert getattr(error.value.orig, "sqlstate", None) == "P6703"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            item = await session.get(BotInstance, first_bot)
            item.status = "registered"
        assert await _count_claims(admin_engine, owner) == 1
        # Falsifying a row's created_at must not bypass trial enrollment.
        spoof_owner = await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username=None, first_name=None,
        )
        spoof_first = await create_owner_project(
            factory, actor_id=spoof_owner, name="Backdated bot origin",
        )
        spoof_second = await create_owner_project(
            factory, actor_id=spoof_owner, name="Second backdated origin",
        )
        await _connect(factory, spoof_owner, spoof_first, backdated=True)
        assert await _count_claims(admin_engine, spoof_owner) == 1
        with pytest.raises(DBAPIError) as error:
            await _connect(factory, spoof_owner, spoof_second, backdated=True)
        assert getattr(error.value.orig, "sqlstate", None) == "P6703"
        # A pre-rollout user's first trial must not be backdated or issued.
        await _connect(factory, old_owner, grandfathered, status="disabled")
        assert await _count_claims(admin_engine, old_owner) == 0
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_combined_free_grant_and_plan_activation_quota_is_per_channel():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner, _, _, project, _, customer_a, channel_a, _ = (
        await setup_subscriber(factory)
    )
    other_users = [
        await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username=None, first_name=None,
        ) for _ in range(3)
    ]
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        extra_customers = [
            ProjectCustomer(project_id=project, user_id=user_id)
            for user_id in other_users
        ]
        other_channel = Resource(
            project_id=project, resource_type="telegram_channel",
            external_id="-100" + str(uuid4().int % 10**12).zfill(12),
            name="Another Channel", status=ResourceStatus.ACTIVE.value,
        )
        session.add_all([*extra_customers, other_channel])
        await session.flush()
        customer_b, customer_c, customer_d = (x.id for x in extra_customers)
        channel_b = other_channel.id
        plan = Plan(
            project_id=project, name="Two-channel premium",
            duration_value=30, duration_unit=DurationUnit.DAY.value,
            status=PlanStatus.ACTIVE.value,
        )
        session.add(plan)
        await session.flush()
        session.add_all([
            PlanResource(project_id=project, plan_id=plan.id, resource_id=channel_a),
            PlanResource(project_id=project, plan_id=plan.id, resource_id=channel_b),
        ])
        plan_id = plan.id
    try:
        await _set_policy(admin_engine, active=True)
        await _connect(factory, owner, project)
        await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_a, resource_id=channel_a, days=7,
        )
        # The same trial owner uses the same three slots regardless of UI
        # path; a multi-channel plan uses one per individual channel.
        activated = await activate_manual_plan(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_b, plan_id=plan_id,
        )
        assert activated.changed
        third = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_c, resource_id=channel_a, days=7,
        )
        assert third.changed
        await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_c, grant_id=third.grant_id,
        )
        with pytest.raises(ValueError, match="Лимит бесплатных активаций"):
            await grant_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=customer_d, resource_id=channel_a, days=7,
            )
        # A different channel still has remaining slots.
        result = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_d, resource_id=channel_b, days=7,
        )
        assert result.changed
        # Revoking a counted free entitlement must never permit a second
        # activation, nor may a revoked manual grant be resurrected by SQL.
        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                old = await session.get(EntitlementGrant, third.grant_id)
                old.status = "active"
                old.revoked_at = None
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                old = await session.get(EntitlementGrant, third.grant_id)
                old.customer_id = customer_d
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
        async with admin_engine.connect() as admin:
            stats = (await admin.execute(text("""
                SELECT channel_chat_id,count(*) AS used
                FROM platform_saas_complimentary_events
                WHERE owner_user_id=:owner GROUP BY channel_chat_id
            """), {"owner": owner})).all()
            assert sorted(int(row.used) for row in stats) == [2, 3]
            assert (await admin.execute(text("""
                SELECT count(*) FROM platform_saas_complimentary_events
                WHERE owner_user_id=:owner AND kind='plan'
            """), {"owner": owner})).scalar_one() == 2
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(func.count(EntitlementGrant.id)).where(
                EntitlementGrant.project_id == project,
                EntitlementGrant.source == EntitlementSource.MANUAL.value,
            )) == 5
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_transfer_recipient_gets_one_trial_not_two(monkeypatch):
    """An ownership transfer must count as a connected bot for its recipient."""
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    current_bot_telegram_id = 0

    async def verified_bot(_token):
        return VerifiedBot(
            telegram_bot_id=current_bot_telegram_id,
            username="trial_test_bot", display_name="Trial transfer",
        )

    async def verify_channel_access(_factory, **_kwargs):
        return None

    monkeypatch.setattr(transfer_service, "verify_telegram_bot", verified_bot)
    monkeypatch.setattr(
        transfer_service, "_verify_recipient_channel_admin",
        verify_channel_access,
    )
    monkeypatch.setattr(
        transfer_service, "encrypt_token",
        lambda token, key: "transfer-test-fresh-credential",
    )
    try:
        sender, recipient, _, first_project, _, _, _, recipient_tid = (
            await setup_subscriber(factory)
        )
        await _set_policy(admin_engine, active=True)
        first_bot = await _connect(factory, sender, first_project)
        async with tenant_session_scope(
            factory, TenantContext(user_id=sender, project_id=first_project),
        ) as session:
            first = await session.get(BotInstance, first_bot)
            current_bot_telegram_id = first.telegram_bot_id
        invite = await transfer_service.create_transfer_invite(
            factory, actor_id=sender, project_id=first_project,
            recipient_telegram_id=recipient_tid,
        )
        accepted = await transfer_service.accept_transfer_invite(
            factory, actor_id=recipient, actor_telegram_id=recipient_tid,
            transfer_id=invite.id, fresh_token="TEST-FRESH-TOKEN",
            encryption_key=None,
        )
        assert accepted.status == "accepted"
        completed = await transfer_service.finalize_transfer(
            factory, actor_id=sender, project_id=first_project,
            transfer_id=invite.id,
        )
        assert completed.status == "completed"
        assert await _count_claims(admin_engine, recipient) == 1

        # Another sender offers the recipient a second bot during 72 hours.
        # It must NOT bypass the recipient's trial just because no new
        # bot_instances row is inserted during completion.
        sender_two, _, _, second_project, _, _, _, _ = (
            await setup_subscriber(factory)
        )
        second_bot = await _connect(factory, sender_two, second_project)
        async with tenant_session_scope(
            factory, TenantContext(user_id=sender_two, project_id=second_project),
        ) as session:
            second = await session.get(BotInstance, second_bot)
            current_bot_telegram_id = second.telegram_bot_id
        second_invite = await transfer_service.create_transfer_invite(
            factory, actor_id=sender_two, project_id=second_project,
            recipient_telegram_id=recipient_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=recipient, actor_telegram_id=recipient_tid,
            transfer_id=second_invite.id, fresh_token="TEST-FRESH-TOKEN-2",
            encryption_key=None,
        )
        with pytest.raises(ValueError, match="72-часового теста"):
            await transfer_service.finalize_transfer(
                factory, actor_id=sender_two, project_id=second_project,
                transfer_id=second_invite.id,
            )
        assert await _count_claims(admin_engine, recipient) == 1
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_pre_rollout_transfer_recipient_remains_grandfathered(monkeypatch):
    """A pre-rollout bot obtained by transfer counts as prior ownership.

    Its original connection event predates the recipient's owner period.
    """
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    telegram_bot_id = 0

    async def verify_bot(_token):
        return VerifiedBot(
            telegram_bot_id=telegram_bot_id,
            username="trial_test_bot", display_name="Transferred legacy bot",
        )

    async def verify_channel(_factory, **_kwargs):
        return None

    monkeypatch.setattr(transfer_service, "verify_telegram_bot", verify_bot)
    monkeypatch.setattr(
        transfer_service, "_verify_recipient_channel_admin", verify_channel,
    )
    monkeypatch.setattr(
        transfer_service, "encrypt_token",
        lambda token, key: "pre-rollout-transfer-test-credential",
    )
    try:
        sender, recipient, _, sender_project, _, _, _, recipient_tid = (
            await setup_subscriber(factory)
        )
        # Connect and transfer entirely BEFORE enabling commercial access.
        source_bot = await _connect(factory, sender, sender_project)
        async with tenant_session_scope(
            factory, TenantContext(user_id=sender, project_id=sender_project),
        ) as session:
            item = await session.get(BotInstance, source_bot)
            telegram_bot_id = item.telegram_bot_id
        invite = await transfer_service.create_transfer_invite(
            factory, actor_id=sender, project_id=sender_project,
            recipient_telegram_id=recipient_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=recipient, actor_telegram_id=recipient_tid,
            transfer_id=invite.id, fresh_token="LEGACY-BOT-TOKEN",
            encryption_key=None,
        )
        completed = await transfer_service.finalize_transfer(
            factory, actor_id=sender, project_id=sender_project,
            transfer_id=invite.id,
        )
        assert completed.status == "completed"
        assert await _count_claims(admin_engine, recipient) == 0

        new_project = await create_owner_project(
            factory, actor_id=recipient, name="Legacy recipient second project",
        )
        await _set_policy(
            admin_engine, active=True, effective_at=datetime.now(UTC),
        )
        second_bot = await _connect(factory, recipient, new_project)
        assert second_bot is not None
        assert await _count_claims(admin_engine, recipient) == 0

        # Grandfathering also protects this recipient from the second
        # pre-rollout-history check in the incoming transfer path.
        other_sender, _, _, other_project, _, _, _, _ = (
            await setup_subscriber(factory)
        )
        extra_bot = await _connect(factory, other_sender, other_project)
        async with tenant_session_scope(
            factory, TenantContext(user_id=other_sender, project_id=other_project),
        ) as session:
            item = await session.get(BotInstance, extra_bot)
            telegram_bot_id = item.telegram_bot_id
        extra_invite = await transfer_service.create_transfer_invite(
            factory, actor_id=other_sender, project_id=other_project,
            recipient_telegram_id=recipient_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=recipient, actor_telegram_id=recipient_tid,
            transfer_id=extra_invite.id, fresh_token="LEGACY-EXTRA-TOKEN",
            encryption_key=None,
        )
        accepted = await transfer_service.finalize_transfer(
            factory, actor_id=other_sender, project_id=other_project,
            transfer_id=extra_invite.id,
        )
        assert accepted.status == "completed"
        assert await _count_claims(admin_engine, recipient) == 0
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_incoming_project_with_two_bot_rows_cannot_start_trial(monkeypatch):
    """A transfer of registered + disabled bots must not get two free slots."""
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    telegram_bot_id = 0

    async def verify_bot(_token):
        return VerifiedBot(
            telegram_bot_id=telegram_bot_id,
            username="trial_test_bot", display_name="Transfer with extra bot",
        )

    async def verify_channel(_factory, **_kwargs):
        return None

    monkeypatch.setattr(transfer_service, "verify_telegram_bot", verify_bot)
    monkeypatch.setattr(
        transfer_service, "_verify_recipient_channel_admin", verify_channel,
    )
    monkeypatch.setattr(
        transfer_service, "encrypt_token",
        lambda token, key: "multi-bot-transfer-test-credential",
    )
    try:
        sender, recipient, _, sender_project, _, _, _, recipient_tid = (
            await setup_subscriber(factory)
        )
        # Before rollout, a merchant is allowed to own multiple bots.
        # A later transfer of this project must not grant both to a
        # brand-new trial recipient.
        registered = await _connect(factory, sender, sender_project)
        await _connect(factory, sender, sender_project, status="disabled")
        async with tenant_session_scope(
            factory, TenantContext(user_id=sender, project_id=sender_project),
        ) as session:
            item = await session.get(BotInstance, registered)
            telegram_bot_id = item.telegram_bot_id

        await _set_policy(
            admin_engine, active=True, effective_at=datetime.now(UTC),
        )
        invite = await transfer_service.create_transfer_invite(
            factory, actor_id=sender, project_id=sender_project,
            recipient_telegram_id=recipient_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=recipient, actor_telegram_id=recipient_tid,
            transfer_id=invite.id, fresh_token="TWO-BOT-TEST-TOKEN",
            encryption_key=None,
        )
        with pytest.raises(ValueError, match="72-часового теста"):
            await transfer_service.finalize_transfer(
                factory, actor_id=sender, project_id=sender_project,
                transfer_id=invite.id,
            )
        assert await _count_claims(admin_engine, recipient) == 0
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()



async def test_delayed_activation_cannot_issue_trial_with_an_existing_bot():
    """An unclaimed post-cutoff bot must reserve the only 72-hour slot.

    A policy enabled after effective_from, or briefly disabled, may leave
    connected bots with no recorded trial. Their owners must not be allowed
    to connect a second bot by claiming a new trial on that second insert.
    """
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username=None, first_name=None,
        )
        first_project = await create_owner_project(
            factory, actor_id=owner, name="Delayed rollout existing bot",
        )
        second_project = await create_owner_project(
            factory, actor_id=owner, name="Delayed rollout second bot",
        )
        # Policy is OFF, although its future configured cutoff will
        # precede the first bot's actual connection.
        # Even while the policy is OFF, spoofed timestamps must not
        # create fake pre-rollout bot_connected audit events.
        first = await _connect(
            factory, owner, first_project, backdated=True,
        )
        assert await _count_claims(admin_engine, owner) == 0
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=first_project),
        ) as session:
            existing = await session.get(BotInstance, first)
            assert existing.created_at >= datetime.now(UTC) - timedelta(minutes=2)
        async with admin_engine.connect() as admin:
            audit_time = (await admin.execute(text("""
                SELECT occurred_at FROM platform_usage_events
                WHERE source_id=:id AND event_type='bot_connected'
            """), {"id": first})).scalar_one()
            assert audit_time >= datetime.now(UTC) - timedelta(minutes=2)
        await _set_policy(
            admin_engine, active=True,
            effective_at=datetime.now(UTC) - timedelta(hours=1),
        )
        with pytest.raises(DBAPIError) as error:
            await _connect(factory, owner, second_project)
        assert getattr(error.value.orig, "sqlstate", None) == "P6703"
        assert await _count_claims(admin_engine, owner) == 0
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=first_project),
        ) as session:
            assert (await session.get(BotInstance, first)) is not None
        # Post-connection edits to created_at cannot rewrite history.
        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=first_project),
            ) as session:
                existing = await session.get(BotInstance, first)
                existing.created_at = datetime.now(UTC) - timedelta(days=45)
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_returning_original_owner_keeps_pre_rollout_grandfathering(
    monkeypatch,
):
    """A project returned after rollout preserves its first owner's history."""
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    telegram_bot_id = 0

    async def verify_bot(_token):
        return VerifiedBot(
            telegram_bot_id=telegram_bot_id,
            username="trial_test_bot",
            display_name="Returned legacy project",
        )

    async def verify_channel(_factory, **_kwargs):
        return None

    monkeypatch.setattr(transfer_service, "verify_telegram_bot", verify_bot)
    monkeypatch.setattr(
        transfer_service, "_verify_recipient_channel_admin", verify_channel,
    )
    monkeypatch.setattr(
        transfer_service, "encrypt_token",
        lambda token, key: "returned-project-credential",
    )
    try:
        original, temporary, _, project, _, _, _, temporary_tid = (
            await setup_subscriber(factory)
        )
        async with admin_engine.connect() as admin:
            original_tid = (await admin.execute(text(
                "SELECT telegram_user_id FROM telegram_users WHERE id=:user"
            ), {"user": original})).scalar_one()
        # Bot is originally connected and handed to a different owner while
        # the commercial policy is still OFF.
        bot_id = await _connect(factory, original, project)
        async with tenant_session_scope(
            factory, TenantContext(user_id=original, project_id=project),
        ) as session:
            bot = await session.get(BotInstance, bot_id)
            telegram_bot_id = bot.telegram_bot_id
        invite = await transfer_service.create_transfer_invite(
            factory, actor_id=original, project_id=project,
            recipient_telegram_id=temporary_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=temporary,
            actor_telegram_id=temporary_tid,
            transfer_id=invite.id, fresh_token="FIRST-LEGACY-TRANSFER",
            encryption_key=None,
        )
        first_transfer = await transfer_service.finalize_transfer(
            factory, actor_id=original, project_id=project,
            transfer_id=invite.id,
        )
        assert first_transfer.status == "completed"

        await _set_policy(
            admin_engine, active=True, effective_at=datetime.now(UTC),
        )
        # The original owner's verified pre-rollout period in the SAME
        # project must be considered before treating a return as new trial.
        return_invite = await transfer_service.create_transfer_invite(
            factory, actor_id=temporary, project_id=project,
            recipient_telegram_id=original_tid,
        )
        await transfer_service.accept_transfer_invite(
            factory, actor_id=original,
            actor_telegram_id=original_tid,
            transfer_id=return_invite.id,
            fresh_token="RETURN-LEGACY-TRANSFER", encryption_key=None,
        )
        return_transfer = await transfer_service.finalize_transfer(
            factory, actor_id=temporary, project_id=project,
            transfer_id=return_invite.id,
        )
        assert return_transfer.status == "completed"
        assert await _count_claims(admin_engine, original) == 0
        # A subsequent second project also remains grandfathered and
        # should never be given a retroactive trial invoice.
        other_project = await create_owner_project(
            factory, actor_id=original, name="Returned owner's project",
        )
        next_bot = await _connect(factory, original, other_project)
        assert next_bot is not None
        assert await _count_claims(admin_engine, original) == 0
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()
