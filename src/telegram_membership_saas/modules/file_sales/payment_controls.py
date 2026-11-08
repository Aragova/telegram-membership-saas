"""Owner-only file-payment toggles per connected bot.

Settings do not update global PaymentMethod.status; those methods continue
to work for subscriptions and other bots. Existing manual orders and Stars
successful payments are processed regardless of the current storefront toggle.
"""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales.payment_control_models import (
    FileSaleMethodVisibility,
)
from telegram_membership_saas.modules.file_sales.service import _owner
from telegram_membership_saas.modules.payments import PaymentMethod, PaymentMethodStatus
from telegram_membership_saas.tenant_context import TenantContext

AUTO_STARS_ID = UUID(int=0)


@dataclass(frozen=True, slots=True)
class FilePaymentChoice:
    method_id: UUID
    name: str
    enabled: bool
    auto_stars: bool = False


async def file_method_enabled(
    session: AsyncSession, *, project_id: UUID, bot_id: int, method_id: UUID,
) -> bool:
    """Missing preference means enabled; explicit False hides new checkouts."""
    value = await session.scalar(select(FileSaleMethodVisibility.enabled).where(
        FileSaleMethodVisibility.project_id == project_id,
        FileSaleMethodVisibility.telegram_bot_id == bot_id,
        FileSaleMethodVisibility.method_id == method_id,
    ))
    return value is not False


async def file_stars_enabled(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int,
) -> bool:
    async with system_project_session_scope(factory, project_id) as session:
        return await file_method_enabled(
            session, project_id=project_id, bot_id=bot_id,
            method_id=AUTO_STARS_ID,
        )


async def _active_manual_methods(
    session: AsyncSession, *, project_id: UUID,
) -> tuple[tuple[UUID, str], ...]:
    return tuple((await session.execute(
        select(PaymentMethod.id, PaymentMethod.name).where(
            PaymentMethod.project_id == project_id,
            PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
        ).order_by(PaymentMethod.sort_order, PaymentMethod.name, PaymentMethod.id)
    )).all())


async def _choices(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
) -> tuple[FilePaymentChoice, ...]:
    methods = await _active_manual_methods(session, project_id=project_id)
    controls = (await session.scalars(select(FileSaleMethodVisibility).where(
        FileSaleMethodVisibility.project_id == project_id,
        FileSaleMethodVisibility.telegram_bot_id == bot_id,
    ))).all()
    flags = {row.method_id: row.enabled for row in controls}
    items = tuple(
        FilePaymentChoice(
            method_id=method_id, name=name,
            enabled=flags.get(method_id, True),
        )
        for method_id, name in methods
    )
    return items + (FilePaymentChoice(
        method_id=AUTO_STARS_ID, name="Telegram Stars (XTR)",
        enabled=flags.get(AUTO_STARS_ID, True), auto_stars=True,
    ),)


async def list_file_payment_controls(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
) -> tuple[FilePaymentChoice, ...]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id,
                     actor_id=actor_id)
        return await _choices(session, project_id=project_id, bot_id=bot_id)


async def _lock_bot(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
) -> None:
    # Serialize concurrent button toggles/bulk changes for this bot.
    instance = await session.scalar(select(BotInstance).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
    ).with_for_update())
    if instance is None:
        raise PermissionError(tr("Бот не найден", "Bot not found"))


async def _set_visibility(
    session: AsyncSession, *, project_id: UUID,
    bot_id: int, method_id: UUID, enabled: bool,
) -> None:
    row = await session.scalar(select(FileSaleMethodVisibility).where(
        FileSaleMethodVisibility.project_id == project_id,
        FileSaleMethodVisibility.telegram_bot_id == bot_id,
        FileSaleMethodVisibility.method_id == method_id,
    ))
    if row is None:
        if not enabled:
            session.add(FileSaleMethodVisibility(
                project_id=project_id, telegram_bot_id=bot_id,
                method_id=method_id, enabled=False,
            ))
    else:
        row.enabled = enabled


async def toggle_file_payment_method(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, actor_id: UUID, method_id: UUID,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id,
                     actor_id=actor_id)
        await _lock_bot(session, project_id=project_id, bot_id=bot_id)
        valid = {x.method_id for x in await _choices(
            session, project_id=project_id, bot_id=bot_id,
        )}
        if method_id not in valid:
            raise ValueError(tr(
            "Этот способ оплаты больше не доступен",
            "This payment method is no longer available",
        ))
        enabled = not await file_method_enabled(
            session, project_id=project_id, bot_id=bot_id,
            method_id=method_id,
        )
        await _set_visibility(
            session, project_id=project_id, bot_id=bot_id,
            method_id=method_id, enabled=enabled,
        )
        return enabled


async def set_all_file_payment_methods(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, actor_id: UUID, enabled: bool,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id,
                     actor_id=actor_id)
        await _lock_bot(session, project_id=project_id, bot_id=bot_id)
        for choice in await _choices(
            session, project_id=project_id, bot_id=bot_id,
        ):
            await _set_visibility(
                session, project_id=project_id, bot_id=bot_id,
                method_id=choice.method_id, enabled=enabled,
            )
