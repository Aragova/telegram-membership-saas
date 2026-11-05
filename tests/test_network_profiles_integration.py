"""Real PostgreSQL RLS and onboarding E2E for independent network copies."""
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots.service import VerifiedBot
from telegram_membership_saas.modules.owner_setup import service as setup
from telegram_membership_saas.modules.owner_setup.network_models import (
    OwnerNetworkProfile,
    ProjectProfileApplication,
)
from telegram_membership_saas.modules.owner_setup.network_profiles import (
    bind_imported_drafts_on_first_channel,
    get_project_import_status,
    import_profile_into_new_project,
    read_owner_network_profile,
    save_owner_network_profile,
    set_network_auto_apply,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentRequest,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.projects import ProjectMember
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.tenant_context import (
    TenantAccessDenied,
    TenantContext,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires real PostgreSQL RLS",
)


def tid():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_network_profile_auto_copies_only_independent_inactive_configuration(
    monkeypatch,
):
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    owner_tg, stranger_tg = tid(), tid()
    key = SecretStr(Fernet.generate_key().decode())
    token = "123456:" + "A" * 35
    try:
        owner = await setup.ensure_telegram_actor(
            factory, telegram_id=owner_tg,
            username="network_owner", first_name="Owner",
        )
        stranger = await setup.ensure_telegram_actor(
            factory, telegram_id=stranger_tg,
            username="network_stranger", first_name="Other",
        )
        original = await setup.create_owner_project(
            factory, actor_id=owner, name="Original network channel",
        )
        original_resource = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=original),
        ) as session:
            session.add(Resource(
                id=original_resource, project_id=original,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-tid()), name="Original private channel",
                status=ResourceStatus.ACTIVE.value,
            ))
        plan_id = await setup.create_owner_plan(
            factory, actor_id=owner, project_id=original,
            name="Premium month", duration_value=1,
            duration_unit="month", resource_id=original_resource,
        )
        await setup.save_owner_price(
            factory, actor_id=owner, project_id=original,
            plan_id=plan_id, currency_code="RUB", amount="550",
        )
        await setup.save_owner_price(
            factory, actor_id=owner, project_id=original,
            plan_id=plan_id, currency_code="USD", amount="7",
        )
        await setup.create_owner_payment_method(
            factory, actor_id=owner, project_id=original,
            name="Bank transfer", currency_code="RUB",
            instruction="Send {amount} {currency} to my account.",
        )
        snapshot = await save_owner_network_profile(
            factory, actor_id=owner, source_project_id=original,
            copy_instructions=True,
        )
        assert (snapshot.enabled, snapshot.version,
                snapshot.plan_count, snapshot.method_count) == (True, 1, 1, 1)
        assert snapshot.source == "Original network channel"
        assert await read_owner_network_profile(
            factory, actor_id=stranger,
        ) is None
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await save_owner_network_profile(
                factory, actor_id=stranger, source_project_id=original,
                copy_instructions=True,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=stranger),
        ) as session:
            assert await session.scalar(select(OwnerNetworkProfile)) is None

        ids = iter((tid(), tid(), tid()))
        async def verified(_token):
            bot_id = next(ids)
            return VerifiedBot(
                telegram_bot_id=bot_id,
                username=f"copied_{bot_id}_bot", display_name="Copied",
            )
        monkeypatch.setattr(setup, "verify_telegram_bot", verified)
        copy_a, _ = await setup.register_new_owner_bot(
            factory, actor_id=owner, actor_telegram_user_id=owner_tg,
            token=token, encryption_key=key, central_bot_id=987654321,
        )
        assert await get_project_import_status(
            factory, actor_id=owner, project_id=copy_a,
        ) == "applied"

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=copy_a),
        ) as session:
            draft = (await session.scalars(
                select(Plan).where(Plan.project_id == copy_a)
            )).all()
            methods = (await session.scalars(
                select(PaymentMethod).where(PaymentMethod.project_id == copy_a)
            )).all()
            prices = (await session.scalars(
                select(PlanPrice).where(PlanPrice.project_id == copy_a)
            )).all()
            links = (await session.scalars(
                select(PlanResource).where(PlanResource.project_id == copy_a)
            )).all()
            payments = (await session.scalars(
                select(PaymentRequest).where(PaymentRequest.project_id == copy_a)
            )).all()
            assert len(draft) == len(methods) == 1
            assert draft[0].id != plan_id
            assert draft[0].name == "Premium month"
            assert draft[0].status == "draft"
            assert not links
            assert not payments
            assert methods[0].status == "disabled"
            assert methods[0].instruction_template == (
                "Send {amount} {currency} to my account."
            )
            assert {p.currency_code: p.amount for p in prices} == {
                "RUB": 550, "USD": 7,
            }
            assert all(p.plan_id == draft[0].id for p in prices)
            codes = (await session.scalars(
                select(PaymentMethodCurrency.currency_code).where(
                    PaymentMethodCurrency.project_id == copy_a,
                )
            )).all()
            assert codes == ["RUB"]

            # Importing the same project a second time cannot duplicate data.
            assert await import_profile_into_new_project(
                session, actor_id=owner, project_id=copy_a,
            )
            assert len((await session.scalars(select(Plan).where(
                Plan.project_id == copy_a,
            ))).all()) == 1

            channel = Resource(
                project_id=copy_a,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-tid()), name="Second channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(channel)
            await session.flush()
            assert await bind_imported_drafts_on_first_channel(
                session, actor_id=owner, project_id=copy_a, resource_id=channel.id,
            ) == 1
            assert await bind_imported_drafts_on_first_channel(
                session, actor_id=owner, project_id=copy_a, resource_id=channel.id,
            ) == 0

        # A newer version affects only future bots, not the copy above.
        newer = await save_owner_network_profile(
            factory, actor_id=owner, source_project_id=original,
            copy_instructions=False,
        )
        assert newer.version == 2
        assert not newer.copy_instructions
        copy_b, _ = await setup.register_new_owner_bot(
            factory, actor_id=owner, actor_telegram_user_id=owner_tg,
            token=token, encryption_key=key, central_bot_id=987654321,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=copy_b),
        ) as session:
            method = await session.scalar(select(PaymentMethod).where(
                PaymentMethod.project_id == copy_b,
            ))
            assert method is not None
            assert "Укажите реквизиты" in method.instruction_template
            app = await session.get(ProjectProfileApplication, copy_b)
            assert app.profile_version == 2
            assert app.status == "applied"
        # A trusted ADMIN can connect the first new channel without being
        # allowed to read the owner's private profile snapshot.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=copy_b),
        ) as session:
            session.add(ProjectMember(
                project_id=copy_b, user_id=stranger, role="admin",
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=stranger, project_id=copy_b),
        ) as session:
            assert await session.scalar(select(OwnerNetworkProfile)) is None
            channel_b = Resource(
                project_id=copy_b,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-tid()), name="Admin verified channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(channel_b)
            await session.flush()
            assert await bind_imported_drafts_on_first_channel(
                session, actor_id=stranger, project_id=copy_b,
                resource_id=channel_b.id,
            ) == 1
        await set_network_auto_apply(factory, actor_id=owner, enabled=False)
        copy_c, _ = await setup.register_new_owner_bot(
            factory, actor_id=owner, actor_telegram_user_id=owner_tg,
            token=token, encryption_key=key, central_bot_id=987654321,
        )
        assert await get_project_import_status(
            factory, actor_id=owner, project_id=copy_c,
        ) is None
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=copy_c),
        ) as session:
            assert not (await session.scalars(
                select(Plan).where(Plan.project_id == copy_c)
            )).all()
    finally:
        await engine.dispose()
