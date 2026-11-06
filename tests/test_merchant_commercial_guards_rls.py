"""Real PostgreSQL checks for prospective merchant commercial safeguards.

No platform trial, quota or commission is enabled by default.
"""
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires privileged migrated PostgreSQL plus RLS runtime",
)


def _tid():
    return uuid4().int % 7_000_000_000_000_000_000 + 1


def _chat():
    return "-100" + str(uuid4().int % 10**12).zfill(12)


async def _bot(factory, owner, project):
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        item = BotInstance(
            id=uuid4(), project_id=project, telegram_bot_id=_tid(),
            username="quota_test_bot", display_name="Quota test",
            token_ciphertext="test-only-encrypted-placeholder",
        )
        session.add(item)
        await session.flush()
        return item.id


async def _merchant(factory, owner, project):
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        return await session.scalar(select(Project.merchant_id).where(
            Project.id == project
        ))


async def test_unconfigured_quota_does_not_restrict_independent_businesses():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        first = await create_owner_project(factory, actor_id=owner, name="First")
        second = await create_owner_project(factory, actor_id=owner, name="Second")
        bot_id = await _bot(factory, owner, first)
        assert await _bot(factory, owner, second)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=first),
        ) as session:
            await session.delete(await session.get(BotInstance, bot_id))
    finally:
        await engine.dispose()


async def test_configured_owner_quota_cannot_be_reset_by_new_project():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner = await ensure_telegram_actor(
        factory, telegram_id=_tid(), username=None, first_name=None,
    )
    other_owner = await ensure_telegram_actor(
        factory, telegram_id=_tid(), username=None, first_name=None,
    )
    try:
        a = await create_owner_project(factory, actor_id=owner, name="First business")
        b = await create_owner_project(factory, actor_id=owner, name="New Merchant ID")
        other = await create_owner_project(
            factory, actor_id=other_owner, name="Independent owner",
        )
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_owner_bot_limits "
                "(owner_user_id, max_connected_bots) VALUES (:owner, 1)"
            ), {"owner": owner})

        bot_id = await _bot(factory, owner, a)
        assert await _bot(factory, other_owner, other)
        with pytest.raises(DBAPIError) as error:
            await _bot(factory, owner, b)
        assert getattr(error.value.orig, "sqlstate", None) == "P6701"

        # A different Merchant ID under the same account is still bounded.
        assert await _merchant(factory, owner, a) != await _merchant(
            factory, owner, b,
        )

        # Existing connected bots may update credentials without consuming
        # another slot; deleting a connection releases the quota.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=a),
        ) as session:
            record = await session.get(BotInstance, bot_id)
            record.token_ciphertext = "updated-test-only-ciphertext"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=a),
        ) as session:
            await session.delete(await session.get(BotInstance, bot_id))
        assert await _bot(factory, owner, b)

        # Privileged limits are entirely opaque to tenant SQL.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=b),
        ) as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(
                    "SELECT * FROM platform_owner_bot_limits"
                ))
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "DELETE FROM platform_owner_bot_limits WHERE owner_user_id=:owner"
            ), {"owner": owner})
        await engine.dispose()
        await admin_engine.dispose()


async def test_simultaneous_registrations_claim_single_account_slot():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner = await ensure_telegram_actor(
        factory, telegram_id=_tid(), username=None, first_name=None,
    )
    try:
        a = await create_owner_project(factory, actor_id=owner, name="Project A")
        b = await create_owner_project(factory, actor_id=owner, name="Project B")
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_owner_bot_limits "
                "(owner_user_id,max_connected_bots) VALUES (:owner,1)"
            ), {"owner": owner})
        results = await asyncio.gather(
            _bot(factory, owner, a), _bot(factory, owner, b),
            return_exceptions=True,
        )
        assert sum(not isinstance(x, BaseException) for x in results) == 1
        failures = [x for x in results if isinstance(x, DBAPIError)]
        assert len(failures) == 1
        assert getattr(failures[0].orig, "sqlstate", None) == "P6701"
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "DELETE FROM platform_owner_bot_limits WHERE owner_user_id=:owner"
            ), {"owner": owner})
        await engine.dispose()
        await admin_engine.dispose()


async def test_trials_are_one_time_per_chosen_scope_and_immutable():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        stranger = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        a = await create_owner_project(factory, actor_id=owner, name="Project A")
        b = await create_owner_project(factory, actor_id=owner, name="Project B")
        ma = await _merchant(factory, owner, a)
        mb = await _merchant(factory, owner, b)

        async with admin_engine.connect() as admin:
            async with admin.begin():
                statement = text(
                    "INSERT INTO merchant_platform_trial_claims "
                    "(id, merchant_id, owner_user_id, policy_key, scope) "
                    "VALUES (:id, :merchant, :owner, :policy, :scope)"
                )

                async def claim(merchant, scope, trial_policy, actor=owner):
                    await admin.execute(statement, {
                        "id": uuid4(), "merchant": merchant,
                        "owner": actor, "policy": trial_policy, "scope": scope,
                    })

                await claim(ma, "owner", "first-owner-trial")
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await claim(mb, "owner", "first-owner-trial")
                assert getattr(error.value.orig, "sqlstate", None) == "23505"

                await claim(ma, "merchant", "merchant-trial")
                await claim(mb, "merchant", "merchant-trial")
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await claim(ma, "merchant", "merchant-trial")
                assert getattr(error.value.orig, "sqlstate", None) == "23505"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await claim(ma, "merchant", "spoof-owner", stranger)
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "UPDATE merchant_platform_trial_claims "
                            "SET policy_key='replacement' WHERE merchant_id=:m"
                        ), {"m": ma})
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                # An explicit rollback makes no trial claims persist in CI.
                await admin.rollback()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=a),
        ) as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(
                    "SELECT * FROM merchant_platform_trial_claims"
                ))
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_future_commission_claim_is_canonical_and_idempotent():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        a = await create_owner_project(factory, actor_id=owner, name="Original")
        b = await create_owner_project(factory, actor_id=owner, name="Other")
        ma = await _merchant(factory, owner, a)
        mb = await _merchant(factory, owner, b)
        payment_id = uuid4()

        async with admin_engine.connect() as admin:
            async with admin.begin():
                first = (await admin.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id,event_type,source_id,currency_code,amount,event_key) "
                    "VALUES (:project,'payment_approved',:source,'RUB',10,:key) "
                    "RETURNING id"
                ), {
                    "project": a, "source": payment_id,
                    "key": f"future-billing-test:{uuid4()}",
                })).scalar_one()
                duplicate = (await admin.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id,event_type,source_id,currency_code,amount,event_key) "
                    "VALUES (:project,'payment_approved',:source,'RUB',10,:key) "
                    "RETURNING id"
                ), {
                    "project": b, "source": payment_id,
                    "key": f"future-billing-test:{uuid4()}",
                })).scalar_one()
                stmt = text(
                    "INSERT INTO merchant_commission_reservations "
                    "(payment_source_id,usage_event_id,merchant_id,policy_key) "
                    "VALUES (:payment,:event,:merchant,'future-policy')"
                )

                async def reserve(event_id, merchant_id):
                    await admin.execute(stmt, {
                        "payment": payment_id, "event": event_id,
                        "merchant": merchant_id,
                    })

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await reserve(duplicate, mb)
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await reserve(first, mb)
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                await reserve(first, ma)
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await reserve(first, ma)
                assert getattr(error.value.orig, "sqlstate", None) == "23505"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "DELETE FROM merchant_commission_reservations "
                            "WHERE payment_source_id=:source"
                        ), {"source": payment_id})
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                # These are reservations only, not actual billing charges.
                await admin.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_cross_owner_chat_reuse_is_flagged_but_not_blocked():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        first = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        transferred = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        a = await create_owner_project(factory, actor_id=first, name="From")
        b = await create_owner_project(factory, actor_id=transferred, name="New owner")
        chat = _chat()
        for owner, project in ((first, a), (transferred, b)):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                session.add(Resource(
                    project_id=project, resource_type="telegram_channel",
                    external_id=chat, name="Transfer candidate",
                ))
        # Reactivating an already-stored inactive channel must also flag
        # transfer history, not only a fresh resource INSERT.
        reactivated_chat = _chat()
        async with tenant_session_scope(
            factory, TenantContext(user_id=transferred, project_id=b),
        ) as session:
            resource = Resource(
                project_id=b, resource_type="telegram_channel",
                external_id=reactivated_chat, name="Reactivated candidate",
                status="disabled",
            )
            session.add(resource)
            await session.flush()
            resource_id = resource.id
        async with tenant_session_scope(
            factory, TenantContext(user_id=first, project_id=a),
        ) as session:
            session.add(Resource(
                project_id=a, resource_type="telegram_channel",
                external_id=reactivated_chat, name="Previous owner",
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=transferred, project_id=b),
        ) as session:
            resource = await session.get(Resource, resource_id)
            resource.status = "active"
        mb = await _merchant(factory, transferred, b)
        async with admin_engine.connect() as admin:
            records = (await admin.execute(text(
                "SELECT count(*) FROM merchant_risk_observations "
                "WHERE merchant_id=:merchant AND telegram_chat_id=:chat "
                "AND risk_kind='historical_chat_other_owner'"
            ), {"merchant": mb, "chat": chat})).scalar_one()
            assert records == 1
            activated = (await admin.execute(text(
                "SELECT count(*) FROM merchant_risk_observations "
                "WHERE merchant_id=:merchant AND telegram_chat_id=:chat"
            ), {"merchant": mb, "chat": reactivated_chat})).scalar_one()
            assert activated == 1

        async with tenant_session_scope(
            factory, TenantContext(user_id=transferred, project_id=b),
        ) as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(
                    "SELECT * FROM merchant_risk_observations"
                ))
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_purging_revenue_bearing_project_cannot_reset_same_channel():
    """A historical paid channel survives hard deletion, without a merchant FK."""
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    key = f"history-guard:{uuid4()}"
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        old = await create_owner_project(factory, actor_id=owner, name="Old")
        fresh = await create_owner_project(factory, actor_id=owner, name="New")
        chat = _chat()

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=old),
        ) as session:
            session.add(Resource(
                project_id=old, resource_type="telegram_channel",
                external_id=chat, name="Historically paid",
            ))

        # The immutable ledger is the evidence after project/payment deletion.
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_usage_events "
                "(project_id,event_type,source_id,currency_code,amount,event_key) "
                "VALUES (:project,'payment_approved',:source,'RUB',10,:key)"
            ), {"project": old, "source": uuid4(), "key": key})

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=old),
        ) as session:
            await session.delete(await session.get(Project, old))

        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=fresh),
            ) as session:
                session.add(Resource(
                    project_id=fresh, resource_type="telegram_channel",
                    external_id=chat, name="Reset attempted",
                ))
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "P6702"
        assert await _merchant(factory, owner, fresh)
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "DELETE FROM platform_usage_events WHERE event_key=:key"
            ), {"key": key})
        await engine.dispose()
        await admin_engine.dispose()
