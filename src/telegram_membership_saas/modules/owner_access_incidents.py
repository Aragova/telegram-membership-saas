"""Owner-only alerts for confirmed Telegram access action incidents.

No edits to access permissions, entitlements or Telegram action state. The
existing project worker scans bounded recent incidents and uses the durable
per-tenant owner notification ledger to deduplicate incidents and recovery.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.access.models import AccessAction, AccessState
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.owner_notifications import (
    OwnerNotificationDelivery,
    _owner_telegram_id,
    _queue,
    read_owner_settings,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid

SCAN_LIMIT = 100
INCIDENT_LOOKBACK = timedelta(days=7)
RECOVERY_LOOKBACK = timedelta(days=30)
RETRY_THRESHOLD = 3
RETRY_AGE = timedelta(minutes=10)


def _is_resolved(state: AccessState) -> bool:
    return state.actual_state == ("granted" if state.desired_access else "revoked")


def _is_confirmed_incident(
    action: AccessAction, state: AccessState, *, now: datetime,
) -> bool:
    """Do not alert for a transient or superseded command.

    A retryable alert's full ten-minute observation window is enforced by
    the persisted notification.available_at, not by queue creation time.
    """
    return (
        state.desired_revision == action.desired_revision
        and not _is_resolved(state)
        and (
            action.status == "failed_permanent"
            or (
                action.status in ("failed_retryable", "processing")
                and action.attempt_count >= RETRY_THRESHOLD
                and action.last_error is not None
            )
        )
    )


async def scan_owner_access_incidents(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID, now: datetime,
) -> int:
    """Record each verified incident once; recovery only after a SENT alert."""
    if now.tzinfo is None:
        raise ValueError("Access-incident scheduler requires aware timestamps")
    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.status != "active":
            return 0
        if not (await read_owner_settings(session, project_id))["access"]:
            return 0
        owner = await _owner_telegram_id(session, project_id)
        if owner is None:
            return 0

        # Filter current revisions, unresolved desired/actual access, and
        # previously queued alerts IN SQL, before LIMIT. Old handled actions
        # must not starve still-unreported failures.
        handled = aliased(OwnerNotificationDelivery)
        active_rows = await session.execute(
            select(AccessAction, AccessState)
            .join(
                AccessState,
                and_(
                    AccessState.project_id == AccessAction.project_id,
                    AccessState.customer_id == AccessAction.customer_id,
                    AccessState.resource_id == AccessAction.resource_id,
                ),
            )
            .where(
                AccessAction.project_id == project_id,
                AccessAction.updated_at >= now - INCIDENT_LOOKBACK,
                AccessAction.desired_revision == AccessState.desired_revision,
                or_(
                    and_(AccessAction.status == "failed_permanent"),
                    and_(
                        AccessAction.status == "failed_retryable",
                        AccessAction.attempt_count >= RETRY_THRESHOLD,
                    ),
                ),
                or_(
                    and_(
                        AccessState.desired_access.is_(True),
                        AccessState.actual_state != "granted",
                    ),
                    and_(
                        AccessState.desired_access.is_(False),
                        AccessState.actual_state != "revoked",
                    ),
                ),
                ~exists(
                    select(handled.id).where(
                        handled.project_id == AccessAction.project_id,
                        handled.kind == "access",
                        handled.event_key == cast(AccessAction.id, String),
                        handled.telegram_id == owner,
                    )
                ),
            )
            .order_by(AccessAction.updated_at.desc(), AccessAction.id)
            .limit(SCAN_LIMIT)
        )
        total = 0
        for action, state in active_rows:
            if not _is_confirmed_incident(action, state, now=now):
                continue
            await _queue(
                session, project_id=project_id, kind="access",
                event_key=str(action.id), recipient=owner,
                payload={"action_id": action.id.hex},
                available_at=(
                    now + RETRY_AGE
                    if action.status == "failed_retryable" else now
                ),
            )
            total += 1

        # Recovery requires proof of a SUCCESSFUL Telegram access action
        # of the original direction, after the owner actually saw a warning.
        # Mere expiry/renewal may flip desired state and is NOT recovery.
        previous_action = aliased(AccessAction)
        success_action = aliased(AccessAction)
        current_state = aliased(AccessState)
        recovery = aliased(OwnerNotificationDelivery)
        proved = exists(
            select(success_action.id).where(
                success_action.project_id == project_id,
                success_action.customer_id == previous_action.customer_id,
                success_action.resource_id == previous_action.resource_id,
                success_action.action_type == previous_action.action_type,
                success_action.status == "succeeded",
                success_action.processed_at >= OwnerNotificationDelivery.sent_at,
            )
        )
        previous = (
            select(OwnerNotificationDelivery, previous_action)
            .join(
                previous_action,
                and_(
                    previous_action.project_id == OwnerNotificationDelivery.project_id,
                    cast(previous_action.id, String) == OwnerNotificationDelivery.event_key,
                ),
            )
            .join(
                current_state,
                and_(
                    current_state.project_id == previous_action.project_id,
                    current_state.customer_id == previous_action.customer_id,
                    current_state.resource_id == previous_action.resource_id,
                ),
            )
            .where(
                OwnerNotificationDelivery.project_id == project_id,
                OwnerNotificationDelivery.kind == "access",
                OwnerNotificationDelivery.status == "sent",
                OwnerNotificationDelivery.error_code.is_(None),
                OwnerNotificationDelivery.sent_at >= now - RECOVERY_LOOKBACK,
                or_(
                    and_(
                        previous_action.action_type == "grant",
                        current_state.desired_access.is_(True),
                        current_state.actual_state == "granted",
                    ),
                    and_(
                        previous_action.action_type == "revoke",
                        current_state.desired_access.is_(False),
                        current_state.actual_state == "revoked",
                    ),
                ),
                proved,
                ~exists(
                    select(recovery.id).where(
                        recovery.project_id == OwnerNotificationDelivery.project_id,
                        recovery.kind == "recovered",
                        recovery.event_key == OwnerNotificationDelivery.event_key,
                        recovery.telegram_id == OwnerNotificationDelivery.telegram_id,
                    )
                ),
            )
            .order_by(OwnerNotificationDelivery.sent_at.desc())
            .limit(SCAN_LIMIT)
        )
        for notice, action in (await session.execute(previous)).all():
            await _queue(
                session, project_id=project_id, kind="recovered",
                event_key=notice.event_key, recipient=owner,
                payload={"action_id": action.id.hex},
            )
            total += 1
        return total


async def _has_verified_recovery(
    session: AsyncSession, *,
    project_id: UUID, action: AccessAction, state: AccessState,
    observed_after: datetime,
) -> bool:
    """Direction-matched successful action, not merely a state revision flip."""
    if state.desired_access != (action.action_type == "grant"):
        return False
    if state.actual_state != ("granted" if action.action_type == "grant" else "revoked"):
        return False
    return await session.scalar(
        select(AccessAction.id).where(
            AccessAction.project_id == project_id,
            AccessAction.customer_id == action.customer_id,
            AccessAction.resource_id == action.resource_id,
            AccessAction.action_type == action.action_type,
            AccessAction.status == "succeeded",
            AccessAction.processed_at >= observed_after,
        ).limit(1)
    ) is not None


async def _current_incident(
    session: AsyncSession, *, project_id: UUID, action_id: UUID,
) -> tuple[AccessAction, AccessState, str, str | None, str | None] | None:
    return (await session.execute(
        select(
            AccessAction, AccessState, Resource.name,
            TelegramUser.username, TelegramUser.first_name,
        )
        .join(
            AccessState,
            and_(
                AccessState.project_id == AccessAction.project_id,
                AccessState.customer_id == AccessAction.customer_id,
                AccessState.resource_id == AccessAction.resource_id,
            ),
        )
        .join(
            Resource,
            and_(
                Resource.id == AccessAction.resource_id,
                Resource.project_id == AccessAction.project_id,
            ),
        )
        .join(
            ProjectCustomer,
            and_(
                ProjectCustomer.id == AccessAction.customer_id,
                ProjectCustomer.project_id == AccessAction.project_id,
            ),
        )
        .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
        .where(
            AccessAction.project_id == project_id,
            AccessAction.id == action_id,
        )
    )).one_or_none()


async def prepare_owner_access_notification(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    recipient: int, payload: dict, recovered: bool, now: datetime,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Revalidate state/owner/mute at send, never reveal raw exception bodies."""
    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.status != "active":
            return None
        if not (await read_owner_settings(session, project_id))["access"]:
            return None
        if (await _owner_telegram_id(session, project_id)) != recipient:
            return None
        try:
            action_id = UUID(hex=payload["action_id"])
        except (ValueError, KeyError, TypeError):
            return None
        found = await _current_incident(
            session, project_id=project_id, action_id=action_id,
        )
        if found is None:
            return None
        action, state, resource_name, username, first_name = found
        if recovered:
            # The old desired operation must actually have succeeded AFTER
            # the Telegram warning. A subscription expiry that merely
            # changes desired_access is never a verified recovery.
            old_alert = await session.scalar(
                select(OwnerNotificationDelivery).where(
                    OwnerNotificationDelivery.project_id == project_id,
                    OwnerNotificationDelivery.kind == "access",
                    OwnerNotificationDelivery.event_key == str(action_id),
                    OwnerNotificationDelivery.telegram_id == recipient,
                    OwnerNotificationDelivery.status == "sent",
                    OwnerNotificationDelivery.error_code.is_(None),
                )
            )
            if (
                old_alert is None
                or old_alert.sent_at is None
                or not await _has_verified_recovery(
                    session, project_id=project_id, action=action, state=state,
                    observed_after=old_alert.sent_at,
                )
            ):
                return None
        elif not _is_confirmed_incident(action, state, now=now):
            return None

        person = (
            "@" + escape(username) if username
            else escape(first_name) if first_name
            else tr("Покупатель", "Customer")
        )
        kind = (
            tr("выдача доступа", "access grant") if action.action_type == "grant"
            else tr("отзыв доступа", "access revocation")
        )
        title = (
            tr("✅ <b>Доступ восстановлен</b>", "✅ <b>Access issue resolved</b>")
            if recovered else
            tr("🚨 <b>Проблема с доступом</b>", "🚨 <b>Access problem detected</b>")
        )
        subtitle = (
            tr("Система подтвердила актуальное состояние.",
               "The system verified the current access state.")
            if recovered else
            tr("Действие не выполнено после проверок. Требуется внимание владельца.",
               "Action remains incomplete after checks. Owner attention required.")
        )
        text = (
            title + "\n\n👤 " + person
            + "\n📣 " + escape(resource_name[:70])
            + "\n" + tr("Действие: ", "Action: ") + kind
            + "\n\n" + subtitle
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=tr("👤 Подписчик", "👤 Subscriber"),
                callback_data="own:subscriber:" + encode_uuid(action.customer_id),
            ),
        ]])
        return text, keyboard
