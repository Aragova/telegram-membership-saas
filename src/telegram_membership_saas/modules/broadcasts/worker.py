"""Slow, durable per-bot Telegram broadcast delivery with safe crash semantics.

Each delivery is recorded as 'sending' BEFORE the API call. If a process
dies after Telegram accepts a message but before the acknowledgement, it is
marked 'unknown' and never sent again automatically. Telegram has no
idempotency key for sendMessage/sendPhoto; exact-once delivery is impossible.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import MessageEntity
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.broadcasts.models import (
    BroadcastAudience,
    BroadcastCampaign,
    BroadcastDelivery,
)

logger = logging.getLogger(__name__)

IDLE_SECONDS = 3.0
ACTIVE_SECONDS = 0.5  # max two Telegram messages/sec per bot, no paid broadcast
STALE_SENDING_SECONDS = 180
MAX_RETRIES = 3


async def _send_post(bot: Bot, *, user_id: int, content) -> None:
    entities = (
        [MessageEntity.model_validate(e) for e in content.entities]
        if content.entities else None
    )
    if content.kind == "text":
        await bot.send_message(
            chat_id=user_id, text=content.text or "", entities=entities,
        )
        return
    fields = {
        "chat_id": user_id, "caption": content.text,
        "caption_entities": entities,
    }
    methods = {
        "photo": ("send_photo", "photo"),
        "video": ("send_video", "video"),
        "document": ("send_document", "document"),
        "animation": ("send_animation", "animation"),
    }
    if content.kind not in methods or not content.file_id:
        raise ValueError("Invalid stored broadcast media")
    method, param = methods[content.kind]
    await getattr(bot, method)(**fields, **{param: content.file_id})


async def deliver_broadcast_once(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, telegram_bot_id: int, bot: Bot,
) -> bool:
    """Process at most one queued delivery for one specific active BotInstance."""
    now = datetime.now(UTC)
    async with system_project_session_scope(factory, project_id) as session:
        # Abandoned in-flight Telegram calls have unknown outcomes.
        # They must not be replayed on worker restart.
        await session.execute(
            update(BroadcastDelivery).where(
                BroadcastDelivery.project_id == project_id,
                BroadcastDelivery.status == "sending",
                BroadcastDelivery.claimed_at < now - timedelta(
                    seconds=STALE_SENDING_SECONDS
                ),
                BroadcastDelivery.campaign_id.in_(
                    select(BroadcastCampaign.id).where(
                        BroadcastCampaign.project_id == project_id,
                        BroadcastCampaign.telegram_bot_id == telegram_bot_id,
                    )
                ),
            ).values(status="unknown", error_code="interrupted")
        )
        row = (await session.execute(
            select(BroadcastDelivery, BroadcastCampaign)
            .join(BroadcastCampaign,
                  BroadcastCampaign.id == BroadcastDelivery.campaign_id)
            .where(
                BroadcastDelivery.project_id == project_id,
                BroadcastCampaign.project_id == project_id,
                BroadcastCampaign.telegram_bot_id == telegram_bot_id,
                BroadcastCampaign.status == "running",
                BroadcastDelivery.status == "pending",
                BroadcastDelivery.next_attempt_at <= now,
            )
            .order_by(
                BroadcastCampaign.created_at,
                BroadcastDelivery.next_attempt_at,
                BroadcastDelivery.id,
            )
            .limit(1)
            .with_for_update(of=BroadcastDelivery, skip_locked=True)
        )).first()
        if row is None:
            # Mark completed campaigns after all confirmed and uncertain
            # deliveries have left the queue. Paused campaigns remain paused.
            await session.execute(
                update(BroadcastCampaign).where(
                    BroadcastCampaign.project_id == project_id,
                    BroadcastCampaign.telegram_bot_id == telegram_bot_id,
                    BroadcastCampaign.status == "running",
                    ~BroadcastCampaign.id.in_(
                        select(BroadcastDelivery.campaign_id).where(
                            BroadcastDelivery.project_id == project_id,
                            BroadcastDelivery.status.in_(("pending", "sending")),
                        )
                    ),
                ).values(status="completed", finished_at=now)
            )
            return False
        delivery, campaign = row
        delivery.status = "sending"
        delivery.claimed_at = now
        delivery.attempts += 1
        claim_id = delivery.id
        user_id = delivery.telegram_user_id
        claimed_at = now

    outcome = "sent"
    error_code: str | None = None
    retry_after: int | None = None
    try:
        await _send_post(bot, user_id=user_id, content=campaign)
    except TelegramForbiddenError:
        outcome, error_code = "blocked", "forbidden"
    except TelegramBadRequest as exc:
        description = str(exc).lower()
        if "chat not found" in description or "user is deactivated" in description:
            outcome, error_code = "blocked", "chat_unavailable"
        else:
            outcome, error_code = "failed", "bad_request"
    except TelegramRetryAfter as exc:
        outcome, error_code = "pending", "rate_limit"
        retry_after = max(1, min(int(exc.retry_after) + 1, 3600))
    except (TelegramNetworkError, TelegramServerError):
        # The API may already have accepted the message. Do not retry.
        outcome, error_code = "unknown", "network_uncertain"
    except Exception:
        # Unexpected ambiguous outcome: don't risk duplicate marketing spam.
        logger.exception("Broadcast API delivery has uncertain outcome")
        outcome, error_code = "unknown", "delivery_uncertain"

    async with system_project_session_scope(factory, project_id) as session:
        record = await session.scalar(select(BroadcastDelivery).where(
            BroadcastDelivery.id == claim_id,
            BroadcastDelivery.project_id == project_id,
            BroadcastDelivery.status == "sending",
            BroadcastDelivery.claimed_at == claimed_at,
        ).with_for_update())
        if record is None:
            # Lease was marked unknown by another worker; no automatic resend.
            return True
        if outcome == "pending" and record.attempts >= MAX_RETRIES:
            outcome, error_code = "failed", "rate_limit_exhausted"
        record.status = outcome
        record.error_code = error_code
        if outcome == "sent":
            record.sent_at = datetime.now(UTC)
        if outcome == "pending":
            record.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=retry_after or 30
            )
        if outcome == "blocked":
            # Do not suppress a newer /start that happened after the send
            # attempt (the user may have unblocked the bot in that interval).
            await session.execute(update(BroadcastAudience).where(
                BroadcastAudience.project_id == project_id,
                BroadcastAudience.telegram_bot_id == telegram_bot_id,
                BroadcastAudience.telegram_user_id == user_id,
                BroadcastAudience.last_started_at <= claimed_at,
            ).values(status="blocked", blocked_at=datetime.now(UTC)))
    return True


async def run_broadcast_loop(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, telegram_bot_id: int, bot: Bot,
) -> None:
    """Optional sibling task to the access worker; never blocks access."""
    while True:
        try:
            sent = await deliver_broadcast_once(
                factory, project_id=project_id,
                telegram_bot_id=telegram_bot_id, bot=bot,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Broadcast delivery iteration failed")
            sent = False
        await asyncio.sleep(ACTIVE_SECONDS if sent else IDLE_SECONDS)
