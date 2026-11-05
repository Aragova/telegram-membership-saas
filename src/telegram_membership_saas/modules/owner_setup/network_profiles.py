"""Opt-in reusable settings for one owner's independently managed Telegram bots.

Only plans/prices and manual payment instructions are snapshotted. The new bot
gets fresh IDs, unbound draft plans and disabled methods; customer balances,
payment requests, subscriptions, membership grants and credentials never move.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.network_models import (
    OwnerNetworkProfile,
    ProjectProfileApplication,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    create_manual_payment_method,
)
from telegram_membership_saas.modules.payments.service import _validate_instruction_template
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice, replace_active_plan_price
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

MAX_PROFILE_ITEMS = 100
EMPTY_INSTRUCTIONS = "Укажите реквизиты для оплаты перед включением этого способа."


@dataclass(frozen=True, slots=True)
class NetworkProfileInfo:
    enabled: bool
    version: int
    plan_count: int
    method_count: int
    source: str
    copy_instructions: bool


async def _source_snapshot(
    session: AsyncSession, *, source_project_id: UUID, copy_instructions: bool,
) -> dict:
    source = await session.scalar(
        select(Project).where(Project.id == source_project_id)
    )
    if source is None:
        raise ValueError("Исходный проект не найден")

    # We only copy current commercial settings. Retired prices, archived
    # plans, credentials and historical transactions are intentionally absent.
    plans = (await session.scalars(
        select(Plan).where(
            Plan.project_id == source_project_id,
            Plan.status.in_(("active", "draft")),
        ).order_by(Plan.sort_order, Plan.id)
    )).all()
    methods = (await session.scalars(
        select(PaymentMethod).where(
            PaymentMethod.project_id == source_project_id,
        ).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )).all()
    if len(plans) > MAX_PROFILE_ITEMS or len(methods) > MAX_PROFILE_ITEMS:
        raise ValueError("В профиле допускается максимум 100 тарифов и 100 способов оплаты")
    if not plans and not methods:
        raise ValueError("Сначала создайте тарифы или способы оплаты")

    prices = (await session.scalars(
        select(PlanPrice).where(
            PlanPrice.project_id == source_project_id,
            PlanPrice.status == "active",
        ).order_by(PlanPrice.currency_code, PlanPrice.id)
    )).all()
    active_prices: dict[UUID, list[dict]] = {}
    for price in prices:
        active_prices.setdefault(price.plan_id, []).append({
            "currency": price.currency_code,
            "amount": str(price.amount),
        })
    links = (await session.scalars(
        select(PaymentMethodCurrency).where(
            PaymentMethodCurrency.project_id == source_project_id,
        ).order_by(PaymentMethodCurrency.currency_code)
    )).all()
    currencies: dict[UUID, list[str]] = {}
    for link in links:
        currencies.setdefault(link.payment_method_id, []).append(link.currency_code)
    return {
        "schema": 1,
        "source": source.name,
        "copy_instructions": copy_instructions,
        "plans": [
            {
                "name": plan.name,
                "duration_value": plan.duration_value,
                "duration_unit": plan.duration_unit,
                "sort_order": plan.sort_order,
                "prices": active_prices.get(plan.id, []),
            }
            for plan in plans
        ],
        "methods": [
            {
                "name": method.name,
                "sort_order": method.sort_order,
                "currencies": currencies.get(method.id, []),
                "instructions": (
                    _validate_instruction_template(method.instruction_template)
                    if copy_instructions else EMPTY_INSTRUCTIONS
                ),
            }
            for method in methods if currencies.get(method.id)
        ],
    }


async def read_owner_network_profile(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
) -> NetworkProfileInfo | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        profile = await session.scalar(select(OwnerNetworkProfile).where(
            OwnerNetworkProfile.owner_user_id == actor_id,
        ))
        if profile is None:
            return None
        data = profile.snapshot
        return NetworkProfileInfo(
            enabled=profile.auto_apply,
            version=profile.version,
            plan_count=len(data["plans"]),
            method_count=len(data["methods"]),
            source=str(data.get("source", "")),
            copy_instructions=bool(data["copy_instructions"]),
        )


async def save_owner_network_profile(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, source_project_id: UUID, copy_instructions: bool,
) -> NetworkProfileInfo:
    """Create/replace an opt-in default profile from an OWNED project.

    Updating the template never modifies already connected bots. One owner
    profile is supported initially; the schema has independent profile UUIDs
    and application revisions to support future multiple named profiles.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=source_project_id),
    ) as session:
        project = await session.scalar(select(Project).where(
            Project.id == source_project_id, Project.owner_user_id == actor_id,
        ))
        if project is None:
            raise PermissionError("Сохранять профиль сети может только владелец проекта")
        snapshot = await _source_snapshot(
            session, source_project_id=source_project_id,
            copy_instructions=copy_instructions,
        )
        # Serialize updates for one owner without leaking another owner's
        # profile. A transaction-level owner advisory lock avoids racing first
        # creation; its namespace is separate from per-project worker leases.
        await session.execute(
            select(func.pg_advisory_xact_lock(
                func.hashtextextended(str(actor_id), 142023)
            ))
        )
        profile = await session.scalar(
            select(OwnerNetworkProfile).where(
                OwnerNetworkProfile.owner_user_id == actor_id,
            ).with_for_update()
        )
        if profile is None:
            profile = OwnerNetworkProfile(
                owner_user_id=actor_id, name="Моя сеть",
                snapshot=snapshot, version=1, auto_apply=True,
            )
            session.add(profile)
        else:
            profile.snapshot = snapshot
            profile.version += 1
            profile.auto_apply = True
            profile.updated_at = func.clock_timestamp()
        await session.flush()
        return NetworkProfileInfo(
            enabled=True, version=profile.version,
            plan_count=len(snapshot["plans"]),
            method_count=len(snapshot["methods"]),
            source=snapshot["source"],
            copy_instructions=copy_instructions,
        )


async def set_network_auto_apply(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, enabled: bool,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        profile = await session.scalar(
            select(OwnerNetworkProfile).where(
                OwnerNetworkProfile.owner_user_id == actor_id,
            ).with_for_update()
        )
        if profile is None:
            raise ValueError("Сначала сохраните профиль сети")
        profile.auto_apply = enabled
        profile.updated_at = func.clock_timestamp()


async def import_profile_into_new_project(
    session: AsyncSession, *, actor_id: UUID, project_id: UUID,
) -> bool:
    """Run inside the new bot registration transaction.

    A failed import rolls back ONLY copied config, never bot credentials.
    Each newly-created project can receive an application at most once.
    """
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id, Project.owner_user_id == actor_id,
        )
    )
    if project is None:
        raise PermissionError("Чужой проект нельзя заполнить профилем")
    profile = await session.scalar(select(OwnerNetworkProfile).where(
        OwnerNetworkProfile.owner_user_id == actor_id,
        OwnerNetworkProfile.auto_apply.is_(True),
    ))
    if profile is None:
        return False
    existing = await session.get(ProjectProfileApplication, project_id)
    if existing is not None:
        return existing.status == "applied"
    # Do not copy source/project IDs from snapshot; each row is rebuilt
    # with a fresh UUID and target project_id.
    try:
        async with session.begin_nested():
            data = profile.snapshot
            if data.get("schema") != 1:
                raise ValueError("Unsupported network profile version")
            for spec in data["plans"]:
                plan = Plan(
                    project_id=project_id,
                    name=spec["name"],
                    duration_value=spec["duration_value"],
                    duration_unit=spec["duration_unit"],
                    sort_order=spec["sort_order"],
                    status="draft",
                )
                session.add(plan)
                await session.flush()
                for price in spec["prices"]:
                    await replace_active_plan_price(
                        session, project_id=project_id, plan_id=plan.id,
                        currency_code=price["currency"],
                        amount=price["amount"],
                    )
            for spec in data["methods"]:
                method = await create_manual_payment_method(
                    session, project_id=project_id,
                    name=spec["name"],
                    currency_codes=spec["currencies"],
                    instruction_template=spec["instructions"],
                    sort_order=spec["sort_order"],
                )
                method.status = "disabled"
            await session.flush()
    except Exception:
        # A newly connected bot must remain operable if a copied currency
        # became unavailable or a profile was otherwise invalidated.
        session.add(ProjectProfileApplication(
            project_id=project_id, owner_user_id=actor_id,
            profile_id=profile.id, profile_version=profile.version,
            status="failed",
        ))
        await session.flush()
        return False
    session.add(ProjectProfileApplication(
        project_id=project_id, owner_user_id=actor_id,
        profile_id=profile.id, profile_version=profile.version,
        status="applied",
    ))
    await session.flush()
    return True


async def bind_imported_drafts_on_first_channel(
    session: AsyncSession, *, actor_id: UUID, project_id: UUID, resource_id: UUID,
) -> int:
    """Attach only imported unbound drafts to the first verified channel."""
    applied = await session.scalar(
        select(ProjectProfileApplication).where(
            ProjectProfileApplication.project_id == project_id,
            ProjectProfileApplication.owner_user_id == actor_id,
            ProjectProfileApplication.status == "applied",
        )
    )
    if applied is None:
        return 0
    channels = (await session.scalars(select(Resource.id).where(
        Resource.project_id == project_id,
        Resource.status == "active",
    ))).all()
    if len(channels) != 1 or channels[0] != resource_id:
        return 0
    drafts = (await session.scalars(
        select(Plan).where(
            Plan.project_id == project_id,
            Plan.status == "draft",
            ~Plan.id.in_(
                select(PlanResource.plan_id).where(
                    PlanResource.project_id == project_id,
                )
            ),
        ).with_for_update()
    )).all()
    for plan in drafts:
        session.add(PlanResource(
            project_id=project_id, plan_id=plan.id, resource_id=resource_id,
        ))
    await session.flush()
    return len(drafts)


async def get_project_import_status(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> str | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        status = await session.scalar(
            select(ProjectProfileApplication.status).where(
                ProjectProfileApplication.project_id == project_id,
                ProjectProfileApplication.owner_user_id == actor_id,
            )
        )
        return status
