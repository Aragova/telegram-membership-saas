"""Project-scoped currency opt-in against an authoritative immutable asset catalog.

Customers do not get to create arbitrary currency codes or change ISO
precision; project managers choose from seeded ISO 4217 fiat currencies and
the platform's already supported cryptocurrencies. Bank payment instructions
remain configured separately per project and per method.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.payments import PaymentMethodCurrency
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    PlanPrice,
    ProjectEnabledCurrency,
)
from telegram_membership_saas.modules.pricing.service import retire_active_plan_price
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

PRIMARY_CURRENCIES = (
    "RUB", "USD", "EUR", "UAH", "BYN", "KZT", "AED", "USDT", "TON", "XTR",
)
CODE_PATTERN = re.compile(r"[A-Z]{3,5}\Z", re.ASCII)
MAX_EXTRA_CURRENCIES = 24


async def project_currency_is_selected(
    session: AsyncSession, *, project_id: UUID, code: str,
) -> bool:
    """Check a currency is usable here, not merely visible globally."""
    if not CODE_PATTERN.fullmatch(code):
        return False
    exists = await session.scalar(select(CurrencyAsset.code).where(
        CurrencyAsset.code == code,
        CurrencyAsset.status == "active",
    ))
    if exists is None:
        return False
    if code in PRIMARY_CURRENCIES:
        return True
    if await session.scalar(select(ProjectEnabledCurrency.currency_code).where(
        ProjectEnabledCurrency.project_id == project_id,
        ProjectEnabledCurrency.currency_code == code,
    )):
        return True
    # Imported Network Profiles may include currencies which were active in
    # the source project but lack an explicit selection in the new project.
    for model in (PlanPrice, PaymentMethodCurrency):
        if await session.scalar(select(model.currency_code).where(
            model.project_id == project_id, model.currency_code == code,
        ).limit(1)):
            return True
    return False


async def list_owner_currencies(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID | None = None,
) -> tuple[str, ...]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        if project_id is not None:
            await _require_project_manager(
                session, project_id=project_id, actor_user_id=actor_id,
            )
        active = set((await session.scalars(select(CurrencyAsset.code).where(
            CurrencyAsset.status == "active",
        ))).all())
        codes = [code for code in PRIMARY_CURRENCIES if code in active]
        if project_id is None:
            return tuple(codes)
        extras = set((await session.scalars(
            select(ProjectEnabledCurrency.currency_code).where(
                ProjectEnabledCurrency.project_id == project_id,
            )
        )).all())
        extras.update((await session.scalars(
            select(PlanPrice.currency_code).where(
                PlanPrice.project_id == project_id,
            ).distinct()
        )).all())
        extras.update((await session.scalars(
            select(PaymentMethodCurrency.currency_code).where(
                PaymentMethodCurrency.project_id == project_id,
            ).distinct()
        )).all())
        return (*codes, *sorted(extras.intersection(active) - set(codes)))


async def enable_owner_currency(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, code: str,
    fiat_only: bool = False,
) -> str:
    """Enable an official catalog currency for just one project.

    The owner enters ONLY the ISO code. The global asset's precision/name/
    status are never modified by tenant input. Idempotent and concurrency safe.
    """
    code = code.strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise ValueError("Введите буквенный код валюты (например, UAH, BYN или GBP)")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        asset = await session.scalar(select(CurrencyAsset).where(
            CurrencyAsset.code == code,
            CurrencyAsset.status == "active",
        ))
        if asset is None:
            raise ValueError(
                "Валюта не найдена в справочнике ISO 4217 / "
                "поддерживаемых криптовалют. Проверьте трёхбуквенный код."
            )
        if fiat_only and (asset.asset_type != "fiat" or code == "XTR"):
            raise ValueError(
                "Для тарифов доступны только обычные валюты. "
                "⭐ Telegram Stars и 💎 Crypto Pay настройте в «Способах оплаты»."
            )
        if code in PRIMARY_CURRENCIES:
            return code
        already = await session.scalar(select(ProjectEnabledCurrency.currency_code).where(
            ProjectEnabledCurrency.project_id == project_id,
            ProjectEnabledCurrency.currency_code == code,
        ))
        if already:
            return code
        count = await session.scalar(select(func.count()).select_from(
            ProjectEnabledCurrency,
        ).where(ProjectEnabledCurrency.project_id == project_id))
        if count >= MAX_EXTRA_CURRENCIES:
            raise ValueError("Достигнут лимит дополнительных валют (24)")
        await session.execute(
            insert(ProjectEnabledCurrency).values(
                project_id=project_id, currency_code=code,
            ).on_conflict_do_nothing(
                index_elements=["project_id", "currency_code"],
            )
        )
        return code


async def list_owner_fiat_currencies(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> tuple[str, ...]:
    """Only fiat prices on tariff cards; native payment rails have own UI.

    Keep existing XTR and crypto price rows and purchase history untouched.
    """
    selected = await list_owner_currencies(
        factory, actor_id=actor_id, project_id=project_id,
    )
    if not selected:
        return ()
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        active_fiat = set((await session.scalars(
            select(CurrencyAsset.code).where(
                CurrencyAsset.code.in_(selected),
                CurrencyAsset.asset_type == "fiat",
                CurrencyAsset.status == "active",
            )
        )).all())
    return tuple(
        code for code in selected if code in active_fiat and code != "XTR"
    )


async def disable_owner_stars_price(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
) -> bool:
    """Disable future native Stars orders; retain historical price evidence."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        retired = await retire_active_plan_price(
            session, project_id=project_id,
            plan_id=plan_id, currency_code="XTR",
        )
        return retired is not None
