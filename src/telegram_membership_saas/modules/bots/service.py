"""Verified, encrypted registration of an owner's customer-facing Telegram bot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

# Never put the token in error messages, logging, URLs, callback_data or the DB unencrypted.
TOKEN_PATTERN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{30,100}$")


@dataclass(frozen=True, slots=True)
class VerifiedBot:
    telegram_bot_id: int
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class BotRegistration:
    telegram_bot_id: int
    username: str
    updated: bool


def _cipher(key: SecretStr | None) -> Fernet:
    if key is None:
        raise RuntimeError("Шифрование токенов ещё не настроено на сервере")
    try:
        return Fernet(key.get_secret_value().encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Ключ шифрования ботов настроен некорректно") from exc


def encrypt_token(token: str, *, key: SecretStr | None) -> str:
    return _cipher(key).encrypt(token.encode("ascii")).decode("ascii")


def decrypt_token(ciphertext: str, *, key: SecretStr | None) -> str:
    """For future trusted bot runtime only; never use in Telegram responses."""
    return _cipher(key).decrypt(ciphertext.encode("ascii")).decode("ascii")


async def _actor_user_id(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
) -> UUID:
    async with session_factory() as session:
        actor = await session.scalar(
            select(TelegramUser.id).where(TelegramUser.telegram_user_id == telegram_user_id)
        )
    if actor is None:
        raise PermissionError("Подключение ботов доступно только владельцу проекта")
    return actor


async def _require_owner(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_id: UUID,
    lock: bool = False,
) -> None:
    stmt = select(Project).where(Project.id == project_id)
    if lock:
        stmt = stmt.with_for_update()
    project = await session.scalar(stmt)
    if (
        project is None
        or project.owner_user_id != actor_id
        or project.status != ProjectStatus.ACTIVE.value
    ):
        raise PermissionError("Подключение ботов доступно только владельцу проекта")


async def require_bot_owner(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    actor_telegram_user_id: int,
) -> None:
    actor_id = await _actor_user_id(session_factory, actor_telegram_user_id)
    try:
        async with tenant_session_scope(
            session_factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            await _require_owner(session, project_id=project_id, actor_id=actor_id)
    except TenantAccessDenied as exc:
        raise PermissionError("Подключение ботов доступно только владельцу проекта") from exc


async def verify_telegram_bot(token: str) -> VerifiedBot:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Неверный формат токена Telegram-бота")
    # getMe validates credentials and returns Telegram's immutable numeric bot identity.
    try:
        candidate = Bot(token=token)
    except Exception:
        # Never preserve raw token or HTTP URL in a traceback chain.
        raise ValueError("Не удалось проверить токен Telegram-бота") from None
    try:
        identity = await candidate.get_me()
    except Exception:
        # Different Telegram transport errors may include a token-bearing URL.
        raise ValueError("Telegram не подтвердил токен бота") from None
    finally:
        try:
            await candidate.session.close()
        except Exception:
            # Closing a failed transport must not disclose a token-bearing URL.
            pass

    if not identity.is_bot or not identity.username:
        raise ValueError("Telegram не вернул корректную учётную запись бота")
    return VerifiedBot(
        telegram_bot_id=identity.id,
        username=identity.username,
        display_name=identity.first_name,
    )


async def register_customer_bot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    actor_telegram_user_id: int,
    token: str,
    encryption_key: SecretStr | None,
    central_bot_id: int,
) -> BotRegistration:
    # Check access first: don't make token-dependent remote requests for strangers.
    await require_bot_owner(
        session_factory, project_id=project_id, actor_telegram_user_id=actor_telegram_user_id
    )
    cipher = _cipher(encryption_key)
    verified = await verify_telegram_bot(token)
    if verified.telegram_bot_id == central_bot_id:
        raise ValueError("Нельзя подключить управляющего бота ViMi Pay как клиентского")

    ciphertext = cipher.encrypt(token.encode("ascii")).decode("ascii")
    actor_id = await _actor_user_id(session_factory, actor_telegram_user_id)
    try:
        async with tenant_session_scope(
            session_factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            # Serialize updates within a project. The global unique DB constraint
            # separately prevents two tenants from claiming the same bot identity.
            await _require_owner(
                session, project_id=project_id, actor_id=actor_id, lock=True
            )
            retiring = await session.scalar(
                select(BotInstance.id).where(
                    BotInstance.project_id == project_id,
                    BotInstance.status == BotInstanceStatus.DELETING.value,
                ).limit(1)
            )
            if retiring is not None:
                raise ValueError(
                    "Предыдущий бот ещё удаляется. Дождитесь завершения "
                    "и подключите бота повторно."
                )
            existing = await session.scalar(
                select(BotInstance).where(
                    BotInstance.project_id == project_id,
                    BotInstance.telegram_bot_id == verified.telegram_bot_id,
                )
            )
            # Until access actions are routed by BotInstance, the
            # project's customer runtime can operate only one registered bot.
            # The project row lock serializes concurrent registrations.
            another_registered = await session.scalar(
                select(BotInstance.id).where(
                    BotInstance.project_id == project_id,
                    BotInstance.status == BotInstanceStatus.REGISTERED.value,
                    BotInstance.telegram_bot_id != verified.telegram_bot_id,
                ).limit(1)
            )
            if another_registered is not None:
                raise ValueError(
                    "В этом проекте уже подключён другой бот. "
                    "Пока поддерживается один бот на проект. "
                    "Для другого бота создайте отдельный проект; "
                    "токен текущего бота можно обновить здесь."
                )
            updated = existing is not None
            if existing is None:
                session.add(
                    BotInstance(
                        project_id=project_id,
                        telegram_bot_id=verified.telegram_bot_id,
                        username=verified.username,
                        display_name=verified.display_name,
                        token_ciphertext=ciphertext,
                        status=BotInstanceStatus.REGISTERED.value,
                    )
                )
            else:
                existing.username = verified.username
                existing.display_name = verified.display_name
                existing.token_ciphertext = ciphertext
                existing.status = BotInstanceStatus.REGISTERED.value
            await session.flush()
    except TenantAccessDenied as exc:
        raise PermissionError("Подключение ботов доступно только владельцу проекта") from exc
    except IntegrityError:
        # Do not disclose the other tenant or SQL parameters in an exception chain.
        raise ValueError("Этот Telegram-бот уже привязан к другому проекту") from None
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "P6711":
            raise ValueError(
                "Подключение нового бота временно ограничено: у владельца "
                "есть просроченный счёт ViMi Pay. Действующие боты и "
                "подписчики продолжают работать. / New bot connections "
                "are paused until the owner's overdue invoice is paid."
            ) from None
        if getattr(exc.orig, "sqlstate", None) == "P6703":
            raise ValueError(
                "Во время бесплатного ознакомления доступен только один бот "
                "(72 часа). / Only one bot is allowed during the 72-hour trial."
            ) from None
        if getattr(exc.orig, "sqlstate", None) == "P6701":
            raise ValueError(
                "Достигнут установленный лимит подключённых ботов. "
                "Удалите ненужное подключение или обратитесь в поддержку ViMi Pay."
            ) from None
        raise
    return BotRegistration(
        telegram_bot_id=verified.telegram_bot_id,
        username=verified.username,
        updated=updated,
    )
