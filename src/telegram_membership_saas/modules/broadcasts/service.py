"""Saved tenant broadcasts: admin ACL, source snapshots and bot-specific audience."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram.types import Message
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.broadcasts.models import (
    BroadcastAudience,
    BroadcastCampaign,
    BroadcastDelivery,
    BroadcastDraft,
    BroadcastTemplate,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
)
from telegram_membership_saas.tenant_context import TenantContext

DRAFT_TTL = timedelta(minutes=20)
TEMPLATE_PAGE_SIZE = 8
HISTORY_PAGE_SIZE = 8
MAX_SAVED_POSTS = 250


def _now() -> datetime:
    return datetime.now(UTC)


async def record_bot_start(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, telegram_bot_id: int, telegram_user_id: int,
) -> None:
    """Record only real private /start for this exact connected bot.

    Replacing a bot does not grant the replacement permission to message
    contacts who interacted only with its predecessor.
    """
    if telegram_user_id <= 0 or telegram_bot_id <= 0:
        return
    async with system_project_session_scope(factory, project_id) as session:
        registered = await session.scalar(
            select(BotInstance.id).where(
                BotInstance.project_id == project_id,
                BotInstance.telegram_bot_id == telegram_bot_id,
                BotInstance.status == "registered",
            )
        )
        if registered is None:
            return
        stmt = insert(BroadcastAudience).values(
            project_id=project_id,
            telegram_bot_id=telegram_bot_id,
            telegram_user_id=telegram_user_id,
            status="active",
        ).on_conflict_do_update(
            index_elements=[
                BroadcastAudience.project_id,
                BroadcastAudience.telegram_bot_id,
                BroadcastAudience.telegram_user_id,
            ],
            set_={
                "status": "active",
                "blocked_at": None,
                "last_started_at": func.now(),
            },
        )
        await session.execute(stmt)


async def _manager(
    session: AsyncSession, *, project_id: UUID, actor_id: UUID,
    telegram_bot_id: int,
) -> None:
    await _require_project_manager(
        session, project_id=project_id, actor_user_id=actor_id,
    )
    bot = await session.scalar(
        select(BotInstance.id).where(
            BotInstance.project_id == project_id,
            BotInstance.telegram_bot_id == telegram_bot_id,
            BotInstance.status == "registered",
        )
    )
    if bot is None:
        raise PermissionError("Подключённый бот недоступен")


async def start_draft(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    mode: str = "create", template_id: UUID | None = None,
) -> None:
    if mode not in ("create", "replace", "rename"):
        raise ValueError("Недопустимый режим редактора")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        if mode != "create":
            if template_id is None or await _get_template(
                session, project_id, telegram_bot_id, template_id,
            ) is None:
                raise ValueError("Пост не найден")
        stmt = insert(BroadcastDraft).values(
            project_id=project_id, actor_user_id=actor_id,
            telegram_bot_id=telegram_bot_id, mode=mode,
            template_id=template_id, expires_at=_now() + DRAFT_TTL,
        ).on_conflict_do_update(
            index_elements=[
                BroadcastDraft.project_id, BroadcastDraft.actor_user_id,
            ],
            set_={
                "telegram_bot_id": telegram_bot_id,
                "mode": mode, "template_id": template_id,
                "expires_at": _now() + DRAFT_TTL,
            },
        )
        await session.execute(stmt)


async def _get_template(
    session: AsyncSession, project_id: UUID,
    telegram_bot_id: int, template_id: UUID,
) -> BroadcastTemplate | None:
    return await session.scalar(select(BroadcastTemplate).where(
        BroadcastTemplate.id == template_id,
        BroadcastTemplate.project_id == project_id,
        BroadcastTemplate.telegram_bot_id == telegram_bot_id,
        BroadcastTemplate.status == "active",
    ))


async def get_draft(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
) -> BroadcastDraft | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        return await session.scalar(select(BroadcastDraft).where(
            BroadcastDraft.project_id == project_id,
            BroadcastDraft.actor_user_id == actor_id,
            BroadcastDraft.telegram_bot_id == telegram_bot_id,
            BroadcastDraft.expires_at > _now(),
        ))


def extract_post(message: Message) -> tuple[str, str | None, list | None, str | None]:
    """Capture the forwarded or authored message in portable Telegram form.

    No receipt, download or source-chat access is retained. Telegram media
    identifiers are valid only for the originating connected bot.
    """
    if message.media_group_id:
        raise ValueError(
            "Альбом из нескольких сообщений пока не поддерживается. "
            "Отправьте одно фото или видео с подписью."
        )
    if message.has_protected_content:
        raise ValueError("Этот пост защищён от копирования в Telegram")
    if message.photo:
        kind, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        kind, file_id = "video", message.video.file_id
    elif message.animation:
        # Telegram sets document as well as animation for legacy clients.
        kind, file_id = "animation", message.animation.file_id
    elif message.document:
        kind, file_id = "document", message.document.file_id
    elif message.text:
        kind, file_id = "text", None
    else:
        raise ValueError("Поддерживаются текст, фото, видео, GIF и документы")
    body = message.text if kind == "text" else message.caption
    if kind == "text" and not body:
        raise ValueError("Пустое сообщение нельзя сохранить")
    entities = message.entities if kind == "text" else message.caption_entities
    serialized = (
        [entity.model_dump(mode="json", exclude_none=True) for entity in entities]
        if entities else None
    )
    return kind, body, serialized, file_id


def extract_album(messages: list[Message]) -> tuple[str, str | None, list | None, None, list]:
    """Serialize one Telegram photo/video group in message order.

    Albums must be delivered as one sendMediaGroup, not partially saved.
    Telegram can send its elements as separate updates in arbitrary order.
    """
    if not 2 <= len(messages) <= 10:
        raise ValueError("Альбом должен содержать от 2 до 10 фото или видео")
    groups = {message.media_group_id for message in messages}
    if len(groups) != 1 or None in groups:
        raise ValueError("Сообщения должны принадлежать одному альбому")
    if len({message.message_id for message in messages}) != len(messages):
        raise ValueError("В альбоме обнаружены повторяющиеся сообщения")
    items = []
    captioned = []
    for message in sorted(messages, key=lambda item: item.message_id):
        if message.has_protected_content:
            raise ValueError("Этот альбом защищён от копирования в Telegram")
        if message.photo:
            items.append({"type": "photo", "file_id": message.photo[-1].file_id})
        elif message.video:
            items.append({"type": "video", "file_id": message.video.file_id})
        else:
            raise ValueError("В альбоме поддерживаются только фото и видео")
        if message.caption or message.caption_entities:
            captioned.append(message)
    if len(captioned) > 1:
        raise ValueError("Используйте одну общую подпись для альбома")
    caption = captioned[0] if captioned else None
    entities = (
        [entity.model_dump(mode="json", exclude_none=True)
         for entity in caption.caption_entities]
        if caption and caption.caption_entities else None
    )
    return "album", caption.caption if caption else None, entities, None, items


async def save_from_message(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    message: Message | None = None, album: list[Message] | None = None,
) -> BroadcastTemplate:
    if (message is None) == (album is None):
        raise ValueError("Передайте один пост или один альбом")
    if album is not None:
        kind, body, entities, file_id, media_items = extract_album(album)
    else:
        kind, body, entities, file_id = extract_post(message)
        media_items = None
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        draft = await session.scalar(
            select(BroadcastDraft).where(
                BroadcastDraft.project_id == project_id,
                BroadcastDraft.actor_user_id == actor_id,
                BroadcastDraft.telegram_bot_id == telegram_bot_id,
                BroadcastDraft.expires_at > _now(),
            ).with_for_update()
        )
        if draft is None or draft.mode not in ("create", "replace"):
            raise ValueError("Редактор закрыт. Начните создание заново.")
        if draft.mode == "create":
            count = await session.scalar(select(func.count()).select_from(
                BroadcastTemplate
            ).where(
                BroadcastTemplate.project_id == project_id,
                BroadcastTemplate.telegram_bot_id == telegram_bot_id,
                BroadcastTemplate.status == "active",
            ))
            if (count or 0) >= MAX_SAVED_POSTS:
                raise ValueError("Лимит библиотеки — 250 постов")
            title = " ".join((body or "").split())[:64] or {
                "photo": "Фотография", "video": "Видео",
                "document": "Документ", "animation": "GIF",
                "album": "Альбом",
            }.get(kind, "Новый пост")
            record = BroadcastTemplate(
                project_id=project_id, telegram_bot_id=telegram_bot_id,
                created_by=actor_id, name=title,
                kind=kind, text=body, entities=entities, file_id=file_id,
                media_items=media_items,
            )
            session.add(record)
        else:
            record = await _get_template(
                session, project_id, telegram_bot_id, draft.template_id,
            )
            if record is None:
                raise ValueError("Пост больше не доступен")
            record.kind = kind
            record.text = body
            record.entities = entities
            record.file_id = file_id
            record.media_items = media_items
        await session.delete(draft)
        await session.flush()
        return record


async def rename_template(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    name: str,
) -> BroadcastTemplate:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 100:
        raise ValueError("Название поста должно быть от 1 до 100 символов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        draft = await session.scalar(select(BroadcastDraft).where(
            BroadcastDraft.project_id == project_id,
            BroadcastDraft.actor_user_id == actor_id,
            BroadcastDraft.telegram_bot_id == telegram_bot_id,
            BroadcastDraft.mode == "rename",
            BroadcastDraft.expires_at > _now(),
        ).with_for_update())
        if draft is None or draft.template_id is None:
            raise ValueError("Начните переименование заново")
        record = await _get_template(
            session, project_id, telegram_bot_id, draft.template_id,
        )
        if record is None:
            raise ValueError("Пост не найден")
        record.name = name
        await session.delete(draft)
        await session.flush()
        return record


async def list_templates(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int, page: int = 0,
) -> tuple[int, tuple[BroadcastTemplate, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Недопустимая страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        cond = (
            BroadcastTemplate.project_id == project_id,
            BroadcastTemplate.telegram_bot_id == telegram_bot_id,
            BroadcastTemplate.status == "active",
        )
        total = await session.scalar(select(func.count()).select_from(
            BroadcastTemplate
        ).where(*cond)) or 0
        posts = (await session.scalars(
            select(BroadcastTemplate).where(*cond)
            .order_by(BroadcastTemplate.created_at.desc(), BroadcastTemplate.id.desc())
            .offset(page * TEMPLATE_PAGE_SIZE).limit(TEMPLATE_PAGE_SIZE)
        )).all()
        return total, tuple(posts)


async def get_template(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    template_id: UUID,
) -> BroadcastTemplate:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        record = await _get_template(
            session, project_id, telegram_bot_id, template_id,
        )
        if record is None:
            raise ValueError("Пост не найден")
        return record


async def archive_template(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    template_id: UUID,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        record = await _get_template(
            session, project_id, telegram_bot_id, template_id,
        )
        if record is None:
            raise ValueError("Пост не найден")
        record.status = "archived"


async def create_campaign(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    template_id: UUID, confirmation_key: UUID,
) -> tuple[BroadcastCampaign, int]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        # Serialize all confirmations for the same project. Two concurrent
        # presses of the same Telegram button must share one campaign.
        await session.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update()
        )
        prior = await session.scalar(select(BroadcastCampaign).where(
            BroadcastCampaign.project_id == project_id,
            BroadcastCampaign.telegram_bot_id == telegram_bot_id,
            BroadcastCampaign.confirmation_key == confirmation_key,
        ))
        if prior is not None:
            if prior.created_by != actor_id or prior.template_id != template_id:
                raise PermissionError("Подтверждение принадлежит другой рассылке")
            total = await session.scalar(select(func.count()).select_from(
                BroadcastDelivery
            ).where(BroadcastDelivery.campaign_id == prior.id)) or 0
            return prior, total
        post = await _get_template(
            session, project_id, telegram_bot_id, template_id,
        )
        if post is None:
            raise ValueError("Пост не найден")
        # Never enqueue two overlapping campaigns; but do not block replay
        # of an earlier confirmation after its campaign has completed.
        existing = await session.scalar(select(BroadcastCampaign.id).where(
            BroadcastCampaign.project_id == project_id,
            BroadcastCampaign.telegram_bot_id == telegram_bot_id,
            BroadcastCampaign.status.in_(("running", "paused")),
        ).limit(1).with_for_update())
        if existing is not None:
            raise ValueError(
                "Другая рассылка ещё не завершена. Остановите или возобновите её."
            )
        campaign = BroadcastCampaign(
            project_id=project_id, telegram_bot_id=telegram_bot_id,
            template_id=post.id, created_by=actor_id,
            confirmation_key=confirmation_key,
            kind=post.kind, text=post.text,
            entities=post.entities, file_id=post.file_id,
            media_items=post.media_items,
        )
        session.add(campaign)
        await session.flush()
        # Recipient list is snapshotted at start; a later /start will
        # participate only in future campaigns.
        recipients = select(
            func.gen_random_uuid(), BroadcastAudience.project_id,
            literal(campaign.id), BroadcastAudience.telegram_user_id,
        ).where(
            BroadcastAudience.project_id == project_id,
            BroadcastAudience.telegram_bot_id == telegram_bot_id,
            BroadcastAudience.status == "active",
        )
        await session.execute(
            insert(BroadcastDelivery).from_select(
                ["id", "project_id", "campaign_id", "telegram_user_id"],
                recipients,
            )
        )
        count = await session.scalar(select(func.count()).select_from(
            BroadcastDelivery
        ).where(BroadcastDelivery.campaign_id == campaign.id)) or 0
        if count == 0:
            campaign.status = "completed"
            campaign.finished_at = _now()
        await session.flush()
        return campaign, count


async def list_campaigns(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    page: int = 0,
) -> tuple[int, tuple[BroadcastCampaign, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Недопустимая страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        clause = (
            BroadcastCampaign.project_id == project_id,
            BroadcastCampaign.telegram_bot_id == telegram_bot_id,
        )
        total = await session.scalar(select(func.count()).select_from(
            BroadcastCampaign
        ).where(*clause)) or 0
        records = (await session.scalars(
            select(BroadcastCampaign).where(*clause)
            .order_by(BroadcastCampaign.created_at.desc(), BroadcastCampaign.id.desc())
            .offset(page * HISTORY_PAGE_SIZE).limit(HISTORY_PAGE_SIZE)
        )).all()
        return total, tuple(records)


async def campaign_stats(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    campaign_id: UUID,
) -> tuple[BroadcastCampaign, dict[str, int]]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        campaign = await session.scalar(select(BroadcastCampaign).where(
            BroadcastCampaign.id == campaign_id,
            BroadcastCampaign.project_id == project_id,
            BroadcastCampaign.telegram_bot_id == telegram_bot_id,
        ))
        if campaign is None:
            raise ValueError("Рассылка не найдена")
        rows = (await session.execute(
            select(BroadcastDelivery.status, func.count())
            .where(BroadcastDelivery.campaign_id == campaign_id)
            .group_by(BroadcastDelivery.status)
        )).all()
        return campaign, dict(rows)


async def set_campaign_status(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
    campaign_id: UUID, status: str,
) -> None:
    if status not in ("paused", "running"):
        raise ValueError("Недопустимое действие")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        record = await session.scalar(select(BroadcastCampaign).where(
            BroadcastCampaign.id == campaign_id,
            BroadcastCampaign.project_id == project_id,
            BroadcastCampaign.telegram_bot_id == telegram_bot_id,
        ).with_for_update())
        if record is None or record.status == "completed":
            raise ValueError("Рассылка завершена")
        record.status = status



async def cancel_draft(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        record = await session.get(BroadcastDraft, (project_id, actor_id))
        if record is not None:
            await session.delete(record)


async def audience_count(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID, telegram_bot_id: int,
) -> int:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _manager(
            session, project_id=project_id, actor_id=actor_id,
            telegram_bot_id=telegram_bot_id,
        )
        return await session.scalar(select(func.count()).select_from(
            BroadcastAudience
        ).where(
            BroadcastAudience.project_id == project_id,
            BroadcastAudience.telegram_bot_id == telegram_bot_id,
            BroadcastAudience.status == "active",
        )) or 0
