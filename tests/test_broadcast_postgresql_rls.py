"""Real PostgreSQL: independent owner audiences and durable broadcast lifecycle."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

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
    BroadcastDelivery,
)
from telegram_membership_saas.modules.broadcasts.service import (
    archive_template,
    audience_count,
    campaign_stats,
    create_campaign,
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

        campaign, count = await create_campaign(
            factory, project_id=project_a, actor_id=owner_a,
            telegram_bot_id=bot_a, template_id=saved.id,
        )
        assert count == 1
        with pytest.raises(ValueError, match="не завершена"):
            await create_campaign(
                factory, project_id=project_a, actor_id=owner_a,
                telegram_bot_id=bot_a, template_id=saved.id,
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
