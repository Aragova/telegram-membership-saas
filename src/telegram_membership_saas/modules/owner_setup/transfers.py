"""Transfer control of exactly one connected bot, never rewrite payment history.

BotFather ownership is managed by Telegram, not its Bot API. The recipient
must already have transferred control in BotFather, prove possession of a
fresh token for the SAME bot and be admin of attached channels.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from aiogram import Bot
from pydantic import SecretStr
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope, tenant_session_scope,
)
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.bots.service import encrypt_token, verify_telegram_bot
from telegram_membership_saas.modules.entitlements.models import (
    EntitlementGrant, EntitlementSource, EntitlementGrantStatus,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.transfer_models import (
    ProjectOwnershipTransfer,
)
from telegram_membership_saas.modules.owner_setup.models import OwnerSetupSession
from telegram_membership_saas.modules.payments.models import (
    PaymentMethod, PaymentMethodStatus, PaymentRequest, PaymentRequestStatus,
)
from telegram_membership_saas.modules.projects import Project, ProjectMember, ProjectStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

TRANSFER_TTL = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class TransferView:
    id: UUID
    project_id: UUID
    bot_id: int
    bot_username: str
    from_user_id: UUID
    to_user_id: UUID
    status: str
    expires_at: datetime


async def _one(
    session: AsyncSession, transfer_id: UUID, *, lock: bool = False,
) -> ProjectOwnershipTransfer:
    q = select(ProjectOwnershipTransfer).where(ProjectOwnershipTransfer.id == transfer_id)
    if lock:
        q = q.with_for_update()
    item = await session.scalar(q)
    if item is None:
        raise PermissionError("Передача недоступна этому аккаунту")
    return item


def _view(item: ProjectOwnershipTransfer) -> TransferView:
    return TransferView(
        id=item.id, project_id=item.project_id,
        bot_id=item.telegram_bot_id, bot_username=item.bot_username,
        from_user_id=item.from_user_id, to_user_id=item.to_user_id,
        status=item.status, expires_at=item.expires_at,
    )


async def list_transfer_invites(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
) -> tuple[TransferView, ...]:
    # Recipient is not yet an owner/member of the project; never bind the
    # project scope here. Transfer-row RLS is restricted to both participants.
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        rows = (await session.scalars(
            select(ProjectOwnershipTransfer).where(
                or_(
                    ProjectOwnershipTransfer.from_user_id == actor_id,
                    ProjectOwnershipTransfer.to_user_id == actor_id,
                ),
                ProjectOwnershipTransfer.status.in_(("pending", "accepted")),
                ProjectOwnershipTransfer.expires_at > datetime.now(UTC),
            ).order_by(ProjectOwnershipTransfer.created_at.desc()).limit(20)
        )).all()
        return tuple(_view(r) for r in rows)


async def create_transfer_invite(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, recipient_telegram_id: int,
) -> TransferView:
    if not 0 < recipient_telegram_id <= 2**63 - 1:
        raise ValueError("Укажите корректный Telegram ID нового владельца")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.owner_user_id == actor_id,
                Project.status == ProjectStatus.ACTIVE.value,
            ).with_for_update()
        )
        if project is None:
            raise PermissionError("Передачу может начать только владелец")
        recipient = await session.scalar(select(TelegramUser).where(
            TelegramUser.telegram_user_id == recipient_telegram_id,
        ))
        if recipient is None:
            raise ValueError(
                "Новый владелец должен открыть этого бота и /start, затем повторите"
            )
        if recipient.id == actor_id:
            raise ValueError("Нельзя передать управление себе")
        bot = await session.scalar(
            select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.status == BotInstanceStatus.REGISTERED.value,
            )
        )
        if bot is None:
            raise ValueError("Сначала подключите действующего бота")
        existing = (await session.scalars(
            select(ProjectOwnershipTransfer).where(
                ProjectOwnershipTransfer.project_id == project_id,
                ProjectOwnershipTransfer.status.in_(("pending", "accepted")),
            ).with_for_update()
        )).all()
        for prior in existing:
            if prior.expires_at > datetime.now(UTC):
                raise ValueError(
                    "Передача уже ожидает ответа. Отмените её перед новой заявкой."
                )
            prior.status = "cancelled"
            prior.cancelled_at = datetime.now(UTC)
            prior.encrypted_new_token = None
        now = datetime.now(UTC)
        transfer = ProjectOwnershipTransfer(
            id=uuid4(), project_id=project_id,
            from_user_id=actor_id, to_user_id=recipient.id,
            telegram_bot_id=bot.telegram_bot_id,
            bot_username=bot.username, status="pending",
            created_at=now, expires_at=now + TRANSFER_TTL,
        )
        session.add(transfer)
        await session.flush()
        return _view(transfer)


async def get_transfer_invite(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, transfer_id: UUID,
) -> TransferView:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        item = await _one(session, transfer_id)
        if actor_id not in (item.from_user_id, item.to_user_id):
            raise PermissionError("Передача недоступна")
        return _view(item)


async def cancel_transfer_invite(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    transfer_id: UUID,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        item = await _one(session, transfer_id, lock=True)
        if actor_id not in (item.from_user_id, item.to_user_id):
            raise PermissionError("Передача недоступна")
        if item.status not in ("pending", "accepted"):
            return False
        item.status = "cancelled"
        item.cancelled_at = datetime.now(UTC)
        item.encrypted_new_token = None
        await session.flush()
        return True


async def _verify_recipient_channel_admin(
    factory: async_sessionmaker[AsyncSession], *,
    transfer: TransferView, recipient_telegram_id: int, token: str,
) -> None:
    # Invitation authorization happens before any worker-scope read.
    async with system_project_session_scope(factory, transfer.project_id) as session:
        project = await session.scalar(select(Project).where(
            Project.id == transfer.project_id,
            Project.owner_user_id == transfer.from_user_id,
            Project.status == ProjectStatus.ACTIVE.value,
        ))
        bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
            BotInstance.project_id == transfer.project_id,
            BotInstance.status == BotInstanceStatus.REGISTERED.value,
        ))
        chats = (await session.scalars(select(Resource.external_id).where(
            Resource.project_id == transfer.project_id,
            Resource.status == ResourceStatus.ACTIVE.value,
        ))).all()
    if project is None or bot_id != transfer.bot_id or not chats:
        raise ValueError("Подключение или привязанные каналы изменились")
    try:
        client = Bot(token=token)
        try:
            for chat in chats:
                member = await client.get_chat_member(chat_id=chat, user_id=recipient_telegram_id)
                role = getattr(member.status, "value", member.status)
                if role not in ("creator", "administrator"):
                    raise ValueError(
                        "Новый владелец должен быть администратором каждого канала"
                    )
        finally:
            await client.session.close()
    except ValueError:
        raise
    except Exception:
        # Never return a token-bearing Telegram transport error to a user.
        raise ValueError(
            "Не удалось проверить права нового владельца в канале. "
            "Бот должен оставаться администратором."
        ) from None


async def accept_transfer_invite(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    actor_telegram_id: int, transfer_id: UUID,
    fresh_token: str, encryption_key: SecretStr | None,
) -> TransferView:
    transfer = await get_transfer_invite(
        factory, actor_id=actor_id, transfer_id=transfer_id,
    )
    if transfer.to_user_id != actor_id or transfer.status != "pending":
        raise PermissionError("Подтвердить передачу может только получатель")
    if transfer.expires_at <= datetime.now(UTC):
        raise ValueError("Срок приглашения истёк")
    verified = await verify_telegram_bot(fresh_token)
    if verified.telegram_bot_id != transfer.bot_id:
        raise ValueError("Токен относится к другому боту. Передача запрещена.")
    await _verify_recipient_channel_admin(
        factory, transfer=transfer,
        recipient_telegram_id=actor_telegram_id, token=fresh_token,
    )
    cipher = encrypt_token(fresh_token, key=encryption_key)
    # Recheck invitation after network IO; recipient's acceptance is durable,
    # but cannot itself change the owner or unlock paid financial actions.
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        item = await _one(session, transfer_id, lock=True)
        if item.status != "pending" or item.to_user_id != actor_id:
            raise ValueError("Приглашение изменилось или отменено")
        if item.expires_at <= datetime.now(UTC):
            raise ValueError("Приглашение истекло")
        item.encrypted_new_token = cipher
        item.accepted_at = datetime.now(UTC)
        item.status = "accepted"
        await session.flush()
        return _view(item)


async def finalize_transfer(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, transfer_id: UUID,
) -> TransferView:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await session.scalar(select(Project).where(
            Project.id == project_id, Project.owner_user_id == actor_id,
            Project.status == ProjectStatus.ACTIVE.value,
        ).with_for_update())
        if project is None:
            raise PermissionError("Завершить передачу может только текущий владелец")
        item = await _one(session, transfer_id, lock=True)
        if (
            item.project_id != project_id or item.from_user_id != actor_id
            or item.status != "accepted"
            or item.expires_at <= datetime.now(UTC)
            or not item.encrypted_new_token
        ):
            raise ValueError("Передача не подтверждена получателем или истекла")
        bot = await session.scalar(select(BotInstance).where(
            BotInstance.project_id == project_id,
            BotInstance.telegram_bot_id == item.telegram_bot_id,
            BotInstance.status == BotInstanceStatus.REGISTERED.value,
        ).with_for_update())
        if bot is None:
            raise ValueError("Бот отключён или заменён")
        incomplete = await session.scalar(select(func.count(PaymentRequest.id)).where(
            PaymentRequest.project_id == project_id,
            PaymentRequest.status.in_((
                PaymentRequestStatus.WAITING_CONFIRMATION.value,
                PaymentRequestStatus.WAITING_REVIEW.value,
            )),
        )) or 0
        if incomplete:
            raise ValueError(
                f"Есть незавершённые счета: {incomplete}. "
                "Закройте их до передачи, чтобы не потерять обязательства."
            )
        # Owner-service rights are tied to the old owner's network and must
        # not accidentally be inherited by an unrelated new business.
        service_rights = await session.scalar(select(EntitlementGrant.id).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
        ).limit(1))
        if service_rights is not None:
            raise ValueError(
                "Есть служебные права старого владельца. "
                "Сначала завершите их безопасно."
            )
        # New owner may have been support staff; a project owner is not a
        # second project_member. Remove their obsolete scoped role.
        member = await session.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == item.to_user_id,
        ).with_for_update())
        if member is not None:
            await session.delete(member)
            await session.flush()
        # A new merchant must explicitly review payee instructions. Disable
        # methods in the SAME transaction; old snapshots/history stay intact.
        await session.execute(
            update(PaymentMethod).where(
                PaymentMethod.project_id == project_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
            ).values(status=PaymentMethodStatus.DISABLED.value)
        )
        bot.token_ciphertext = item.encrypted_new_token
        await session.flush()
        # SQL UPDATE without RETURNING: old owner stops seeing the project
        # under FORCE RLS as soon as the ownership change is committed.
        result = await session.execute(
            update(Project)
            .where(Project.id == project_id, Project.owner_user_id == actor_id)
            .values(owner_user_id=item.to_user_id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise RuntimeError("Владелец изменился. Передача отменена")
        # Merchant SECURITY DEFINER project trigger atomically closes the
        # old owner's historical period, opens the new one, and consumes the
        # ACCEPTED transfer (clearing the encrypted pending token).
        return TransferView(
            id=item.id, project_id=item.project_id, bot_id=item.telegram_bot_id,
            bot_username=item.bot_username,
            from_user_id=item.from_user_id, to_user_id=item.to_user_id,
            status="completed", expires_at=item.expires_at,
        )
