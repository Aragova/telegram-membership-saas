import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentProvider,
    create_manual_payment_method,
    set_payment_method_currencies,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for payment method integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username="payment_owner")
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, user_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=user_id)) as session:
        project = Project(owner_user_id=user_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def test_manual_payment_methods_are_currency_aware_and_tenant_safe() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        user_id = await _create_user(session_factory)
        project_a = await _create_project(session_factory, user_id, "Payments A")
        project_b = await _create_project(session_factory, user_id, "Payments B")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            method = await create_manual_payment_method(
                session,
                project_id=project_a,
                name="USDT TRC20",
                currency_codes=["usdt"],
                instruction_template=(
                    "Отправьте {amount} {currency} по реквизитам TRC20. "
                    "Тариф: {plan}; период: {period}."
                ),
            )
            assert method.provider == PaymentProvider.MANUAL_SETTLEMENT.value

            await set_payment_method_currencies(
                session,
                project_id=project_a,
                payment_method_id=method.id,
                currency_codes=["USDT", "TON"],
            )
            method_id = method.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            methods = list((await session.scalars(select(PaymentMethod))).all())
            assert [item.id for item in methods] == [method_id]
            currencies = set(
                (
                    await session.scalars(
                        select(PaymentMethodCurrency.currency_code).where(
                            PaymentMethodCurrency.payment_method_id == method_id
                        )
                    )
                ).all()
            )
            assert currencies == {"USDT", "TON"}

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(PaymentMethod))).all()) == []
            assert list((await session.scalars(select(PaymentMethodCurrency))).all()) == []

            with pytest.raises(ValueError, match="not accessible"):
                await set_payment_method_currencies(
                    session,
                    project_id=project_b,
                    payment_method_id=method_id,
                    currency_codes=["USDT"],
                )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            with pytest.raises(ValueError, match="Unknown or disabled"):
                await create_manual_payment_method(
                    session,
                    project_id=project_a,
                    name="Unknown currency",
                    currency_codes=["XYZ"],
                    instruction_template="Оплатите {amount} {currency}",
                )
    finally:
        await engine.dispose()
