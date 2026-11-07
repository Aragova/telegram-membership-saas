"""Real PostgreSQL: independent owner audiences and durable broadcast lifecycle."""
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.broadcasts.models import (
    BroadcastAudience,
    BroadcastCampaign,
    BroadcastDelivery,
)
from telegram_membership_saas.modules.broadcasts.service import (
    archive_template,
    audience_count,
    campaign_stats,
    create_campaign,
    get_draft,
    get_template,
    list_templates,
    record_bot_start,
    save_from_message,
    set_campaign_status,
    start_draft,
)
from telegram_membership_saas.modules.broadcasts.worker import (
    deliver_broadcast_once,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires real migrated PostgreSQL",
)


def _tid():
    return uuid4().int % 7_000_000_000_000_000_000 + 1


async def _project_with_bot(factory):
    owner = await ensure_telegram_actor(
        factory, telegram_id=_tid(), username=None, first_name=None,
    )
    project = await create_owner_project(
        factory, actor_id=owner, name="Broadcast isolation business",
    )
    bot_id = _tid()
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        session.add(BotInstance(
            project_id=project, telegram_bot_id=bot_id,
            username="stage_broadcast_bot", display_name="Broadcast",
            token_ciphertext="test-only-unusable-token",
        ))
    return owner, project, bot_id


def _post(body):
    return SimpleNamespace(
        text=body, caption=None, entities=None, caption_entities=None,
        photo=None, video=None, document=None, animation=None,
        media_group_id=None, has_protected_content=False,
    )


async def test_real_bot_audience_saved_posts_and_delivery_are_tenant_isolated():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_a, project_a, bot_a = await _project_with_bot(factory)
        owner_b, project_b, bot_b = await _project_with_bot(factory)
        buyer = _tid()
        stranger = _tid()
        # Buyer A is not a paying customer; /start alone opts into audience.
        await record_bot_start(
            factory, project_id=project_a, telegram_bot_id=bot_a,
            telegram_user_id=buyer,
        )
        await record_bot_start(
            factory, project_id=project_a, telegram_bot_id=bot_a,
            telegram_user_id=buyer,
        )
        await record_bot_start(
            factory, project_id=project_b, telegram_bot_id=bot_b,
            telegram_user_id=stranger,
        )
        assert await audience_count(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a,
        ) == 1
        assert await audience_count(
            factory, project_id=project_b, actor_id=owner_b,
            telegram_bot_id=bot_b,
        ) == 1
        # Historical /start of one bot is not permission for another bot.
        await record_bot_start(
            factory, project_id=project_a, telegram_bot_id=bot_b,
            telegram_user_id=stranger,
        )
        assert await audience_count(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a,
        ) == 1

        await start_draft(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a,
        )
        saved = await save_from_message(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, message=_post("Скидка 🎉 t.me/offer"),
        )
        assert "Скидка" in saved.text
        total, posts = await list_templates(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a,
        )
        assert total == 1 and posts[0].id == saved.id

        with pytest.raises((PermissionError, ValueError, TenantAccessDenied)):
            await get_template(
                factory, project_id=project_a, actor_id=owner_b,
                telegram_bot_id=bot_a, template_id=saved.id,
            )
        with pytest.raises((PermissionError, ValueError, TenantAccessDenied)):
            await get_template(
                factory, project_id=project_b, actor_id=owner_b,
                telegram_bot_id=bot_b, template_id=saved.id,
            )

        first_confirmation_key = uuid4()
        campaign, count = await create_campaign(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, template_id=saved.id,
            confirmation_key=first_confirmation_key,
        )
        assert count == 1
        with pytest.raises(ValueError, match="не завершена"):
            await create_campaign(
                factory, project_id=project_a, actor_id=owner_a,
                telegram_bot_id=bot_a, template_id=saved.id,
                confirmation_key=uuid4(),
            )
        # Later /start joins only the next send-to-all confirmation.
        await record_bot_start(
            factory, project_id=project_a, telegram_bot_id=bot_a,
            telegram_user_id=_tid(),
        )
        _, initial = await campaign_stats(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, campaign_id=campaign.id,
        )
        assert initial == {"pending": 1}

        await set_campaign_status(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, campaign_id=campaign.id, status="paused",
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        assert not await deliver_broadcast_once(
            factory, project_id=project_a, telegram_bot_id=bot_a, bot=bot,
        )
        bot.send_message.assert_not_awaited()
        await set_campaign_status(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, campaign_id=campaign.id, status="running",
        )
        assert await deliver_broadcast_once(
            factory, project_id=project_a, telegram_bot_id=bot_a, bot=bot,
        )
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["chat_id"] == buyer
        assert not await deliver_broadcast_once(
            factory, project_id=project_a, telegram_bot_id=bot_a, bot=bot,
        )
        _, results = await campaign_stats(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, campaign_id=campaign.id,
        )
        assert results == {"sent": 1}
        # Replaying the exact confirmation after full completion is
        # idempotent: it must not enqueue another wave of messages.
        previous, old_count = await create_campaign(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, template_id=saved.id,
            confirmation_key=first_confirmation_key,
        )
        assert previous.id == campaign.id and old_count == 1
        assert not await deliver_broadcast_once(
            factory, project_id=project_a, telegram_bot_id=bot_a, bot=bot,
        )
        bot.send_message.assert_awaited_once()

        # Deleting a saved template never erases previous campaign evidence.
        await archive_template(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, template_id=saved.id,
        )
        archived_total, _ = await list_templates(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a,
        )
        assert archived_total == 0
        _, results = await campaign_stats(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, campaign_id=campaign.id,
        )
        assert results == {"sent": 1}

        async with system_project_session_scope(factory, project_b) as session:
            assert not (await session.scalars(select(
                BroadcastDelivery
            ).where(BroadcastDelivery.campaign_id == campaign.id))).all()
            assert not (await session.scalars(select(
                BroadcastAudience
            ).where(BroadcastAudience.project_id == project_a))).all()
    finally:
        await engine.dispose()



async def test_album_template_and_campaign_keep_independent_snapshots():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, project, bot_id = await _project_with_bot(factory)
        buyer = _tid()
        await record_bot_start(
            factory, project_id=project, telegram_bot_id=bot_id,
            telegram_user_id=buyer,
        )
        await start_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        first = _post(None)
        first.message_id = 101
        first.media_group_id = "group-abc"
        first.photo = [SimpleNamespace(file_id="photo-owned-by-bot")]
        first.caption = "Caption 🎉"
        second = _post(None)
        second.message_id = 102
        second.media_group_id = "group-abc"
        second.video = SimpleNamespace(file_id="video-owned-by-bot")
        saved = await save_from_message(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, album=[second, first],
        )
        assert saved.kind == "album"
        assert saved.media_items == [
            {"type": "photo", "file_id": "photo-owned-by-bot"},
            {"type": "video", "file_id": "video-owned-by-bot"},
        ]
        campaign, count = await create_campaign(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, template_id=saved.id,
            confirmation_key=uuid4(),
        )
        assert count == 1
        assert campaign.kind == "album"
        assert campaign.text == "Caption 🎉"
        assert campaign.media_items == saved.media_items

        bot = SimpleNamespace(send_media_group=AsyncMock())
        assert await deliver_broadcast_once(
            factory, project_id=project, telegram_bot_id=bot_id, bot=bot,
        )
        bot.send_media_group.assert_awaited_once()
        _, stats = await campaign_stats(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, campaign_id=campaign.id,
        )
        assert stats == {"sent": 1}

        # The campaign payload stays available after library removal.
        await archive_template(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, template_id=saved.id,
        )
        async with system_project_session_scope(factory, project) as session:
            stored = await session.get(BroadcastCampaign, campaign.id)
            assert stored.media_items == campaign.media_items
    finally:
        await engine.dispose()


async def test_pending_album_cannot_overwrite_another_editor_draft():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, project, bot_id = await _project_with_bot(factory)
        await start_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        existing = await save_from_message(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, message=_post("Original content"),
        )
        # The group starts while the owner is creating a new post.
        await start_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        old_draft = await get_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        first = _post(None)
        first.message_id = 1
        first.media_group_id = "old-group"
        first.photo = [SimpleNamespace(file_id="photo-1")]
        second = _post(None)
        second.message_id = 2
        second.media_group_id = "old-group"
        second.photo = [SimpleNamespace(file_id="photo-2")]

        # The owner switches editor before the album aggregation flush.
        await start_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, mode="replace", template_id=existing.id,
        )
        with pytest.raises(ValueError, match="изменился"):
            await save_from_message(
                factory, project_id=project, actor_id=owner,
                telegram_bot_id=bot_id, album=[first, second],
                expected_draft=(
                    old_draft.mode, old_draft.template_id, old_draft.expires_at,
                ),
            )
        unchanged = await get_template(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, template_id=existing.id,
        )
        assert unchanged.text == "Original content"
        active = await get_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        assert active.mode == "replace" and active.template_id == existing.id
    finally:
        await engine.dispose()


async def test_telegram_429_pauses_every_pending_recipient_for_this_bot():
    """A per-recipient 429 delay is insufficient: the whole queue must stop."""
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, project, bot_id = await _project_with_bot(factory)
        for _ in range(3):
            await record_bot_start(
                factory, project_id=project, telegram_bot_id=bot_id,
                telegram_user_id=_tid(),
            )
        await start_draft(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id,
        )
        template = await save_from_message(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, message=_post("Promo"),
        )
        campaign, count = await create_campaign(
            factory, project_id=project, actor_id=owner,
            telegram_bot_id=bot_id, template_id=template.id,
            confirmation_key=uuid4(),
        )
        assert count == 3
        retry = TelegramRetryAfter(
            method=SendMessage(chat_id=123, text="Promo"),
            message="Bot flood control", retry_after=120,
        )
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=retry))
        assert await deliver_broadcast_once(
            factory, project_id=project, telegram_bot_id=bot_id, bot=bot,
        )
        bot.send_message.assert_awaited_once()

        async with system_project_session_scope(factory, project) as session:
            row = await session.get(BroadcastCampaign, campaign.id)
            assert row.cooldown_until > datetime.now(UTC) + timedelta(seconds=100)
            deliveries = (await session.scalars(select(
                BroadcastDelivery
            ).where(BroadcastDelivery.campaign_id == campaign.id))).all()
            assert sum(v.status == "pending" for v in deliveries) == 3

        bot.send_message = AsyncMock()
        # There are still two *other* recipients ready for immediate
        # delivery. The bot-wide cooldown must stop them as well.
        assert not await deliver_broadcast_once(
            factory, project_id=project, telegram_bot_id=bot_id, bot=bot,
        )
        bot.send_message.assert_not_awaited()

        async with system_project_session_scope(factory, project) as session:
            await session.execute(update(BroadcastCampaign).where(
                BroadcastCampaign.id == campaign.id,
            ).values(cooldown_until=datetime.now(UTC) - timedelta(seconds=1)))
        assert await deliver_broadcast_once(
            factory, project_id=project, telegram_bot_id=bot_id, bot=bot,
        )
        bot.send_message.assert_awaited_once()
    finally:
        await engine.dispose()
