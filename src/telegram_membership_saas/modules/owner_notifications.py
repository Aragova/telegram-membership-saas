"""Per-project owner alerts. Queue writes are transactional with business events.

No Telegram send is part of a payment transaction. No receipt bytes are stored.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID, uuid4

from aiogram import Bot
from aiogram.exceptions import AiogramError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.db_base import Base
from telegram_membership_saas.localization import current_language, recipient_language, tr
from telegram_membership_saas.modules.identity.models import TelegramUser
from telegram_membership_saas.modules.projects.models import Project


class OwnerNotificationSettings(Base):
    __tablename__ = "owner_notification_settings"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    invoice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    receipt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    decision_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    expiry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    access_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    daily_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OwnerNotificationDelivery(Base):
    __tablename__ = "owner_notification_deliveries"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "event_key", "telegram_id",
                         name="uq_owner_notification_delivery_event_recipient"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending", default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


SETTINGS_FIELDS = frozenset({
    "invoice", "receipt", "decision", "expiry", "access", "daily",
})


async def read_owner_settings(session: AsyncSession, project_id: UUID) -> dict[str, bool]:
    row = await session.scalar(
        select(OwnerNotificationSettings).where(OwnerNotificationSettings.project_id == project_id)
    )
    return {
        name: bool(getattr(row, name + "_enabled")) if row is not None else name != "daily"
        for name in SETTINGS_FIELDS
    }


async def set_owner_setting(
    session: AsyncSession, *, project_id: UUID, actor_id: UUID,
    category: str, enabled: bool,
) -> None:
    """Tenant-scope alone is insufficient: settings are reserved for the real owner."""
    if category not in SETTINGS_FIELDS:
        raise ValueError("Unsupported notification category")
    owner_id = await session.scalar(
        select(Project.owner_user_id).where(Project.id == project_id)
    )
    if owner_id is None or owner_id != actor_id:
        raise PermissionError("Only the actual project owner can change alerts")
    await session.execute(
        insert(OwnerNotificationSettings)
        .values(project_id=project_id, **{category + "_enabled": enabled})
        .on_conflict_do_update(
            index_elements=[OwnerNotificationSettings.project_id],
            set_={category + "_enabled": enabled, "updated_at": func.now()},
        )
    )


async def _owner_telegram_id(session: AsyncSession, project_id: UUID) -> int | None:
    return await session.scalar(
        select(TelegramUser.telegram_user_id)
        .join(Project, Project.owner_user_id == TelegramUser.id)
        .where(Project.id == project_id)
    )


async def _queue(
    session: AsyncSession, *, project_id: UUID, kind: str,
    event_key: str, recipient: int, payload: dict,
) -> None:
    await session.execute(
        insert(OwnerNotificationDelivery)
        .values(
            project_id=project_id, kind=kind, event_key=event_key,
            telegram_id=recipient, payload=payload,
        )
        .on_conflict_do_nothing(
            index_elements=[
                OwnerNotificationDelivery.project_id,
                OwnerNotificationDelivery.kind,
                OwnerNotificationDelivery.event_key,
                OwnerNotificationDelivery.telegram_id,
            ]
        )
    )


async def queue_invoice_created(
    session: AsyncSession, *, project_id: UUID, request_id: UUID,
    customer_id: UUID, username: str | None, first_name: str | None,
    plan_name: str, duration_value: int | None, duration_unit: str,
    amount: str, currency: str, method: str,
) -> None:
    """Exactly one queued event for this real request, committed with checkout."""
    preferences = await read_owner_settings(session, project_id)
    if not preferences["invoice"]:
        return
    recipient = await _owner_telegram_id(session, project_id)
    if recipient is None:
        return
    await _queue(
        session, project_id=project_id, kind="invoice",
        event_key=request_id.hex, recipient=recipient,
        payload={
            "request_id": request_id.hex, "customer_id": customer_id.hex,
            "username": username or "", "first_name": first_name or "",
            "plan_name": plan_name, "duration_value": duration_value,
            "duration_unit": duration_unit, "amount": amount,
            "currency": currency, "method": method,
        },
    )


def _invoice_text(payload: dict) -> str:
    # Never interpolate unescaped Telegram/user-controlled HTML into messages.
    from telegram_membership_saas.modules.telegram_commerce.service import (
        format_period, is_period_only_plan_name,
    )

    username = payload.get("username")
    first_name = payload.get("first_name")
    person = ("@" + escape(username) if username else tr("Покупатель", "Customer"))
    if first_name:
        person += " · " + escape(first_name)
    period = format_period(payload["duration_value"], payload["duration_unit"])
    custom_title = payload.get("plan_name") or ""
    if is_period_only_plan_name(custom_title, payload["duration_value"], payload["duration_unit"]):
        label = period
    else:
        label = escape(custom_title) + " · " + escape(period)
    invoice_ref = str(payload["request_id"])[:10].upper()
    return (
        tr("🧾 <b>Счёт сформирован</b>\n\n", "🧾 <b>Payment invoice created</b>\n\n")
        + "👤 " + person + "\n"
        + tr("📦 Подписка: ", "📦 Subscription: ") + label + "\n"
        + "💳 <b>" + escape(payload["amount"]) + " "
        + escape(payload["currency"]) + "</b> · "
        + tr("Полуавтомат", "Manual payment") + "\n"
        + tr("🕒 Ожидает оплату", "🕒 Awaiting payment") + "\n\n"
        + tr("№ ", "Ref ") + invoice_ref
    )


def _invoice_keyboard(payload: dict) -> InlineKeyboardMarkup:
    # Existing bot-scoped subscriber and review callbacks perform role checks.
    from telegram_membership_saas.modules.telegram_commerce.service import decode_uuid, encode_uuid

    customer = UUID(hex=payload["customer_id"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr("👤 Подписчик", "👤 Subscriber"),
            callback_data="own:subscriber:" + encode_uuid(customer),
        )],
        [InlineKeyboardButton(
            text=tr("📋 Заявки", "📋 Requests"),
            callback_data="own:reviews",
        )],
    ])


async def deliver_one_owner_notification(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot: Bot, now: datetime | None = None,
) -> bool:
    """One per-loop leased delivery. Telegram transport never holds a DB lock."""
    moment = now or datetime.now(UTC)
    async with system_project_session_scope(factory, project_id) as session:
        candidate = await session.scalar(
            select(OwnerNotificationDelivery)
            .where(
                OwnerNotificationDelivery.project_id == project_id,
                (
                    ((OwnerNotificationDelivery.status == "pending")
                     & (OwnerNotificationDelivery.available_at <= moment))
                    | ((OwnerNotificationDelivery.status == "processing")
                       & (OwnerNotificationDelivery.locked_at <= moment - timedelta(minutes=5)))
                ),
            )
            .order_by(OwnerNotificationDelivery.available_at, OwnerNotificationDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            return False
        candidate.status = "processing"
        candidate.locked_at = moment
        candidate.attempt_count += 1
        ident, kind = candidate.id, candidate.kind
        payload = dict(candidate.payload)
        recipient = candidate.telegram_id
        attempt = candidate.attempt_count

    sent = False
    permanent = False
    error_code: str | None = None
    try:
        async with recipient_language(factory, recipient):
            if kind == "invoice":
                await bot.send_message(
                    recipient, _invoice_text(payload), parse_mode="HTML",
                    reply_markup=_invoice_keyboard(payload),
                )
            else:
                # New producers must be explicitly registered before using them.
                raise ValueError("Unrecognized owner notification kind")
        sent = True
    except (AiogramError, ValueError) as exc:
        error_code = exc.__class__.__name__
        permanent = isinstance(exc, (TelegramForbiddenError, ValueError))

    async with system_project_session_scope(factory, project_id) as session:
        row = await session.scalar(
            select(OwnerNotificationDelivery).where(
                OwnerNotificationDelivery.id == ident,
                OwnerNotificationDelivery.project_id == project_id,
                OwnerNotificationDelivery.status == "processing",
                OwnerNotificationDelivery.attempt_count == attempt,
            ).with_for_update()
        )
        if row is not None:
            row.locked_at = None
            row.error_code = error_code
            if sent:
                row.status = "sent"
                row.sent_at = datetime.now(UTC)
            elif permanent or attempt >= 5:
                row.status = "failed"
            else:
                row.status = "pending"
                row.available_at = datetime.now(UTC) + timedelta(seconds=min(30 * 2 ** (attempt - 1), 600))
    return True
