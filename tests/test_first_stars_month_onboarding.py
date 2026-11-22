"""Live-schema quick Stars onboarding without test buttons or lasting bots.

The entire owner/channel/plan/pricing fixture is in an outer transaction
with SAVEPOINT-backed helper sessions. A second connection verifies rollback.
"""
import os
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup import service as setup
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires PostgreSQL transaction with clean rollback",
)


async def test_first_stars_month_plan_is_published_once_with_real_checkout():
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, _, project, _, _ = await _project(factory, _tid())
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    resource = await session.scalar(select(Resource).where(
                        Resource.project_id == project,
                    ))
                    assert resource is not None
                    resource_id = resource.id
                # A consumed invalid Stars reply can safely advance its
                # original generation, without restarting channel selection.
                generation = await setup.set_flow(
                    factory, actor_id=owner, step="stars_month_price",
                    project_id=project, payload={"resource_id": resource_id.hex},
                )
                await setup.bind_prompt(
                    factory, actor_id=owner, step="stars_month_price",
                    message_id=1201, generation=generation,
                )
                invalid = await setup.consume_flow(
                    factory, actor_id=owner, reply_message_id=None,
                )
                assert invalid.step == "stars_month_price"
                retry = await setup.advance_flow(
                    factory, actor_id=owner, source=invalid,
                    step="stars_month_price",
                    payload={"resource_id": resource_id.hex},
                )
                await setup.bind_prompt(
                    factory, actor_id=owner, step="stars_month_price",
                    message_id=1202, generation=retry,
                )
                corrected = await setup.consume_flow(
                    factory, actor_id=owner, reply_message_id=None,
                )
                assert corrected.generation == retry
                assert corrected.project_id == project
                with pytest.raises(ValueError, match="Проект или шаг"):
                    await setup.advance_flow(
                        factory, actor_id=owner, source=invalid,
                        step="stars_month_price", payload=invalid.payload,
                    )
                with pytest.raises(ValueError, match="250000"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=resource_id, stars_amount="1.5",
                    )
                plan_id = await setup.create_one_month_stars_plan(
                    factory, actor_id=owner, project_id=project,
                    resource_id=resource_id, stars_amount="47",
                )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    plan = await session.get(Plan, plan_id)
                    assert (plan.duration_value, plan.duration_unit, plan.status) == (
                        1, "month", "active",
                    )
                    price = await session.scalar(select(PlanPrice).where(
                        PlanPrice.project_id == project,
                        PlanPrice.plan_id == plan_id,
                        PlanPrice.currency_code == "XTR",
                        PlanPrice.status == "active",
                    ))
                    assert price is not None
                    assert price.amount == Decimal("47")
                    assert (await session.scalar(select(PlanResource.resource_id).where(
                        PlanResource.plan_id == plan_id,
                    ))) == resource_id
                overview = await setup.setup_overview(
                    factory, actor_id=owner, project_id=project,
                )
                assert overview.sales_configured
                assert len(overview.plans) == 1
                with pytest.raises(ValueError, match="Тариф уже создан"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=resource_id, stars_amount="47",
                    )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    assert len((await session.scalars(select(Plan).where(
                        Plan.project_id == project,
                        Plan.removed_at.is_(None),
                    ))).all()) == 1
                other_owner, _, _, _, _ = await _project(factory, _tid())
                with pytest.raises((ValueError, PermissionError)):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=other_owner, project_id=project,
                        resource_id=resource_id, stars_amount="89",
                    )
            finally:
                await tx.rollback()
        async with admin.connect() as verify:
            assert (await verify.execute(text(
                "SELECT count(*) FROM projects WHERE id=:id"
            ), {"id": project})).scalar_one() == 0
    finally:
        await admin.dispose()



async def test_first_native_channel_plan_is_atomic_and_group_is_rejected():
    """A native link and price publish together, never as an interim bot sale."""
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, _, project, bot_id, _ = await _project(factory, _tid())
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    resource = await session.scalar(select(Resource).where(
                        Resource.project_id == project,
                    ))
                    bot = await session.get(BotInstance, bot_id)
                    group = Resource(
                        project_id=project, resource_type="telegram_group",
                        external_id="-100" + str(_tid()),
                        name="Not a channel", status="active",
                    )
                    session.add(group)
                    await session.flush()
                    channel_id, chat_id, telegram_bot_id = (
                        resource.id, int(resource.external_id),
                        bot.telegram_bot_id,
                    )
                assert await setup.list_native_stars_setup_channels(
                    factory, actor_id=owner, project_id=project,
                    telegram_bot_id=telegram_bot_id,
                ) == ((channel_id, resource.name),)
                with pytest.raises(ValueError, match="приватном канале"):
                    await setup.native_stars_setup_chat(
                        factory, actor_id=owner, project_id=project,
                        resource_id=group.id,
                        telegram_bot_id=telegram_bot_id,
                    )
                assert await setup.native_stars_setup_chat(
                    factory, actor_id=owner, project_id=project,
                    resource_id=channel_id,
                    telegram_bot_id=telegram_bot_id,
                ) == chat_id
                with pytest.raises(ValueError, match="10000"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=channel_id, stars_amount="10001",
                        native_invite_link="https://t.me/+valid-test",
                        native_bot_id=telegram_bot_id, native_chat_id=chat_id,
                    )
                with pytest.raises(ValueError, match="только"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=group.id, stars_amount="77",
                        native_invite_link="https://t.me/+valid-test",
                        native_bot_id=telegram_bot_id, native_chat_id=chat_id,
                    )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    assert not (await session.scalars(select(Plan).where(
                        Plan.project_id == project,
                    ))).all()

                # Sending text must not invalidate a button-only confirmation
                # or strand its Create link / Back callbacks.
                confirmation = await setup.set_flow(
                    factory, actor_id=owner, project_id=project,
                    step="stars_native_confirm",
                    payload={"resource_id": channel_id.hex, "stars": 77},
                )
                await setup.bind_prompt(
                    factory, actor_id=owner, step="stars_native_confirm",
                    message_id=1203, generation=confirmation,
                )
                assert not await setup.is_expected_prompt(
                    factory, actor_id=owner, reply_message_id=None,
                )
                assert await setup.consume_flow(
                    factory, actor_id=owner, reply_message_id=None,
                ) is None
                assert (await setup.consume_button_flow(
                    factory, actor_id=owner,
                    expected_step="stars_native_confirm",
                    generation=confirmation, discard=True,
                )).payload["stars"] == 77

                plan_id = await setup.create_one_month_stars_plan(
                    factory, actor_id=owner, project_id=project,
                    resource_id=channel_id, stars_amount="77",
                    native_invite_link="https://t.me/+valid-test",
                    native_bot_id=telegram_bot_id, native_chat_id=chat_id,
                )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    plan = await session.get(Plan, plan_id)
                    price = await session.scalar(select(PlanPrice).where(
                        PlanPrice.project_id == project,
                        PlanPrice.plan_id == plan_id,
                        PlanPrice.status == "active",
                    ))
                    assert plan.status == "active"
                    assert price.amount == Decimal("77")
                    assert price.native_subscription_active is True
                    assert price.native_subscription_link == "https://t.me/+valid-test"
                    assert price.native_subscription_chat_id == str(chat_id)
                    assert price.native_subscription_bot_id == telegram_bot_id
                with pytest.raises(ValueError, match="Тариф уже создан"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=channel_id, stars_amount="77",
                        native_invite_link="https://t.me/+valid-test",
                        native_bot_id=telegram_bot_id, native_chat_id=chat_id,
                    )
            finally:
                await tx.rollback()
        async with admin.connect() as verify:
            assert (await verify.execute(text(
                "SELECT count(*) FROM plans WHERE name='1 месяц'"
            ))).scalar_one() == 0
    finally:
        await admin.dispose()
