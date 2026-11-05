"""PostgreSQL integration: UAH/BYN + owner-selected ISO currency handling."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.currencies import (
    enable_owner_currency,
    list_owner_currencies,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    save_owner_price,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    attach_owner_method_currency,
    get_owner_plan_info,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
)
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    PlanPrice,
    ProjectEnabledCurrency,
)
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires real PostgreSQL RLS",
)


def user_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_uah_byn_are_ready_and_iso_currency_is_project_scoped():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=user_id(),
            username="owner_currency_a", first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=user_id(),
            username="owner_currency_b", first_name="Other",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Hryvnia test",
        )
        other = await create_owner_project(
            factory, actor_id=outsider, name="Isolated wallet",
        )
        enabled = await list_owner_currencies(
            factory, actor_id=owner, project_id=project,
        )
        assert {"UAH", "BYN", "RUB", "USD"}.issubset(set(enabled))
        assert "JPY" not in enabled
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            resource = Resource(
                project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-user_id()), name="Currency channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()
            resource_id = resource.id
        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Premium", duration_value=1, duration_unit="month",
            resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="UAH", amount="150.25",
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="BYN", amount="12.30",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Ukrainian card", currency_code="UAH",
            instruction="Transfer {amount} {currency} to verified account.",
        )
        info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        assert {row.currency: row.precision for row in info.prices} == {
            "BYN": 2, "UAH": 2,
        }

        with pytest.raises(ValueError, match="Сначала добавьте валюту"):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                method_id = await session.scalar(
                    select(PaymentMethod.id).where(
                        PaymentMethod.project_id == project,
                    )
                )
            await attach_owner_method_currency(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, currency_code="JPY",
            )

        assert await enable_owner_currency(
            factory, actor_id=owner, project_id=project, code=" jpy ",
        ) == "JPY"
        assert await enable_owner_currency(
            factory, actor_id=owner, project_id=project, code="JPY",
        ) == "JPY"
        assert "JPY" in await list_owner_currencies(
            factory, actor_id=owner, project_id=project,
        )
        assert "JPY" not in await list_owner_currencies(
            factory, actor_id=outsider, project_id=other,
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await enable_owner_currency(
                factory, actor_id=outsider, project_id=project, code="GBP",
            )
        with pytest.raises(ValueError, match="не найдена"):
            await enable_owner_currency(
                factory, actor_id=owner, project_id=project, code="ZZZ",
            )
        with pytest.raises(ValueError, match="Введите буквенный код"):
            await enable_owner_currency(
                factory, actor_id=owner, project_id=project, code="USD;DROP",
            )
        with pytest.raises(ValueError, match="fractional"):
            await save_owner_price(
                factory, actor_id=owner, project_id=project,
                plan_id=plan, currency_code="JPY", amount="120.5",
            )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="JPY", amount="120",
        )
        await attach_owner_method_currency(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, currency_code="JPY",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(CurrencyAsset.precision).where(
                CurrencyAsset.code == "JPY",
            )) == 0
            assert await session.scalar(select(PlanPrice.id).where(
                PlanPrice.project_id == project,
                PlanPrice.currency_code == "JPY",
            )) is not None
            assert await session.scalar(select(PaymentMethodCurrency.currency_code).where(
                PaymentMethodCurrency.project_id == project,
                PaymentMethodCurrency.currency_code == "JPY",
            )) == "JPY"
            rows = (await session.scalars(
                select(ProjectEnabledCurrency.currency_code).where(
                    ProjectEnabledCurrency.project_id == project,
                )
            )).all()
            assert rows == ["JPY"]
        async with tenant_session_scope(
            factory, TenantContext(user_id=outsider, project_id=other),
        ) as session:
            assert not (await session.scalars(
                select(ProjectEnabledCurrency).where(
                    ProjectEnabledCurrency.project_id == project,
                )
            )).all()
    finally:
        await engine.dispose()
