"""Project identity and owner wizard replies must be scoped to a physical bot."""
import os
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import (
    bind_prompt,
    can_manage_project,
    choose_project,
    consume_flow,
    create_owner_project,
    ensure_telegram_actor,
    get_selected_project,
    is_expected_prompt,
    owner_home,
    set_flow,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Needs PostgreSQL tenant RLS and migration 0018",
)


def _id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_bot_scoped_owner_wizards_are_independent_of_central_selection():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_id(), username="v2_owner",
            first_name="Owner",
        )
        other = await ensure_telegram_actor(
            factory, telegram_id=_id(), username="v2_outsider",
            first_name="Outsider",
        )
        alpha = await create_owner_project(
            factory, actor_id=owner, name="Project alpha",
        )
        beta = await create_owner_project(
            factory, actor_id=owner, name="Project beta",
        )
        assert await can_manage_project(
            factory, actor_id=owner, project_id=beta,
        )
        assert not await can_manage_project(
            factory, actor_id=other, project_id=beta,
        )
        with pytest.raises(PermissionError):
            await owner_home(
                factory, actor_id=other,
                fixed_project_id=beta, bot_scope_id=707,
            )

        await choose_project(
            factory, actor_id=owner, project_id=alpha,
        )
        await owner_home(
            factory, actor_id=owner,
            fixed_project_id=beta, bot_scope_id=707,
        )
        assert await get_selected_project(
            factory, actor_id=owner, bot_scope_id=0,
        ) == alpha
        assert await get_selected_project(
            factory, actor_id=owner, bot_scope_id=707,
            fixed_project_id=beta,
        ) == beta

        central_generation = await set_flow(
            factory, actor_id=owner, step="bot_token", bot_scope_id=0,
        )
        await bind_prompt(
            factory, actor_id=owner, step="bot_token",
            message_id=100, generation=central_generation, bot_scope_id=0,
        )
        customer_generation = await set_flow(
            factory, actor_id=owner, step="plan_name",
            project_id=beta, bot_scope_id=707,
        )
        await bind_prompt(
            factory, actor_id=owner, step="plan_name",
            message_id=100, generation=customer_generation,
            bot_scope_id=707,
        )
        assert await is_expected_prompt(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=0,
        )
        assert await is_expected_prompt(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=707,
        )
        assert not await is_expected_prompt(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=808,
        )

        flow = await consume_flow(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=707,
        )
        assert flow.project_id == beta
        assert flow.step == "plan_name"
        assert await is_expected_prompt(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=0,
        )
        central = await consume_flow(
            factory, actor_id=owner,
            reply_message_id=100, bot_scope_id=0,
        )
        assert central.step == "bot_token"
        assert central.project_id == alpha
    finally:
        await engine.dispose()
