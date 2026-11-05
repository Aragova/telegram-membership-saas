"""Real PostgreSQL E2E for a *new owner's* setup, with Telegram API stubbed.

This is the administrator path; existing purchase-path tests stay separate.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import encrypt_token
from telegram_membership_saas.modules.owner_setup import service as setup
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.projects import ProjectMember
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL required for real tenant onboarding integration",
)


def _new_telegram_id():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_owner_configures_project_channel_tariff_payment_and_publishes(monkeypatch):
    engine = build_engine(Settings())
    sf = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode())
    raw_token = "123456:" + "A" * 35
    try:
        telegram_id = _new_telegram_id()
        actor_id = await setup.ensure_telegram_actor(
            sf, telegram_id=telegram_id,
            username="new_owner", first_name="Owner",
        )

        empty = await setup.owner_home(sf, actor_id=actor_id)
        assert empty.projects == ()
        assert empty.selected is None

        project_id = await setup.create_owner_project(
            sf, actor_id=actor_id, name=" My Paid Channel "
        )
        home = await setup.owner_home(sf, actor_id=actor_id)
        assert home.selected == project_id
        assert home.projects == ((project_id, "My Paid Channel"),)

        generation = await setup.set_flow(
            sf, actor_id=actor_id, step="plan_name"
        )
        await setup.bind_prompt(
            sf, actor_id=actor_id, step="plan_name",
            message_id=123, generation=generation,
        )
        assert await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=777
        ) is None
        consumed = await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=123
        )
        assert consumed.step == "plan_name"
        assert consumed.project_id == project_id
        assert await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=123
        ) is None

        # Plain input replaces ForceReply: only the active, bot-scoped
        # prompt may consume the next message; no stale prompt persists.
        plain = await setup.set_flow(
            sf, actor_id=actor_id, step="method_name",
            payload={"currency": "RUB"},
        )
        assert await setup.is_expected_prompt(
            sf, actor_id=actor_id, reply_message_id=None,
        ) is False  # prompt not yet bound
        await setup.bind_prompt(
            sf, actor_id=actor_id, step="method_name",
            message_id=124, generation=plain,
        )
        assert await setup.is_expected_prompt(
            sf, actor_id=actor_id, reply_message_id=None,
        ) is True
        assert await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=123,
        ) is None  # stale replies are rejected
        plain_flow = await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=None,
        )
        assert plain_flow.step == "method_name"
        assert await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=None,
        ) is None

        # Navigation clears a pending text prompt before a stray message
        # could overwrite buyer-facing bank instructions.
        edit_generation = await setup.set_flow(
            sf, actor_id=actor_id, step="method_edit",
            payload={"method_id": uuid4().hex},
        )
        await setup.bind_prompt(
            sf, actor_id=actor_id, step="method_edit",
            message_id=125, generation=edit_generation,
        )
        assert await setup.cancel_owner_flow(
            sf, actor_id=actor_id,
        ) is True
        assert await setup.is_expected_prompt(
            sf, actor_id=actor_id, reply_message_id=None,
        ) is False
        assert await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=None,
        ) is None

        old = await setup.set_flow(
            sf, actor_id=actor_id, step="plan_duration",
            payload={"name": "Old plan"},
        )
        new = await setup.set_flow(
            sf, actor_id=actor_id, step="plan_duration",
            payload={"name": "Current plan"},
        )
        assert old != new
        with pytest.raises(ValueError, match="заменён"):
            await setup.bind_prompt(
                sf, actor_id=actor_id, step="plan_duration",
                message_id=900, generation=old,
            )
        with pytest.raises(ValueError, match="устарел"):
            await setup.consume_button_flow(
                sf, actor_id=actor_id, expected_step="plan_duration",
                generation=old,
            )
        current = await setup.consume_button_flow(
            sf, actor_id=actor_id, expected_step="plan_duration",
            generation=new,
        )
        assert current.payload["name"] == "Current plan"

        # A finished prompt must not reattach to a different project after
        # a project switch or a new wizard has superseded the previous one.
        first = await setup.set_flow(
            sf, actor_id=actor_id, step="plan_name", payload={}
        )
        await setup.bind_prompt(
            sf, actor_id=actor_id, step="plan_name",
            message_id=1001, generation=first,
        )
        ended = await setup.consume_flow(
            sf, actor_id=actor_id, reply_message_id=1001
        )
        await setup.set_flow(
            sf, actor_id=actor_id, step="method_name", payload={"currency": "RUB"}
        )
        with pytest.raises(ValueError, match="Проект или шаг"):
            await setup.advance_flow(
                sf, actor_id=actor_id, source=ended,
                step="plan_duration", payload={"name": "No stale plan"},
            )

        async with tenant_session_scope(
            sf, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            session.add(BotInstance(
                project_id=project_id, telegram_bot_id=990001,
                username="owner_paid_bot", display_name="Owned Bot",
                token_ciphertext=encrypt_token(raw_token, key=key),
                status="registered",
            ))

        own_member = SimpleNamespace(
            status="administrator",
            can_invite_users=True, can_restrict_members=True,
        )
        personal = SimpleNamespace(status="creator")
        long_title = "К" * 128
        bot = SimpleNamespace(
            id=990001,
            get_chat=AsyncMock(return_value=SimpleNamespace(
                id=-100987654322, type="channel", title=long_title,
            )),
            get_me=AsyncMock(return_value=SimpleNamespace(id=990001)),
            get_chat_member=AsyncMock(
                side_effect=lambda chat_id, user_id: (
                    own_member if user_id == 990001 else personal
                )
            ),
            session=SimpleNamespace(close=AsyncMock()),
        )
        monkeypatch.setattr(setup, "get_settings", lambda: SimpleNamespace(
            bot_token_encryption_key=key
        ))
        monkeypatch.setattr(setup, "Bot", lambda token: bot)
        resource = await setup.verify_channel_and_add(
            sf, actor_id=actor_id, actor_telegram_id=telegram_id,
            project_id=project_id, chat_reference=-100987654322,
        )
        assert resource.name == long_title[:120]
        assert len(resource.name) == 120
        assert resource.external_id == "-100987654322"

        plan_id = await setup.create_owner_plan(
            sf, actor_id=actor_id, project_id=project_id,
            name="Premium Monthly",
            duration_value=1, duration_unit="month",
            resource_id=resource.id,
        )
        unnamed_id = await setup.create_owner_plan(
            sf, actor_id=actor_id, project_id=project_id,
            name="   ", duration_value=1, duration_unit="month",
            resource_id=resource.id,
        )
        async with tenant_session_scope(
            sf, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            unnamed = await session.get(Plan, unnamed_id)
            assert unnamed.name == "1 месяц"
            assert unnamed.status == "draft"

        await setup.save_owner_price(
            sf, actor_id=actor_id, project_id=project_id,
            plan_id=plan_id, currency_code="RUB", amount="499",
        )
        with pytest.raises(ValueError, match="способ оплаты"):
            await setup.publish_owner_plan(
                sf, actor_id=actor_id, project_id=project_id, plan_id=plan_id
            )
        await setup.create_owner_payment_method(
            sf, actor_id=actor_id, project_id=project_id,
            name="СБП", currency_code="RUB",
            instruction="Отправьте {amount} {currency} на тестовый счёт.",
        )
        published = await setup.publish_owner_plan(
            sf, actor_id=actor_id, project_id=project_id, plan_id=plan_id
        )
        assert published == "Premium Monthly"

        overview = await setup.setup_overview(
            sf, actor_id=actor_id, project_id=project_id
        )
        assert overview.timezone == "Europe/Moscow"
        assert overview.bots == (("owner_paid_bot", "registered"),)
        assert len(overview.resources) == len(overview.methods) == 1
        assert (plan_id, "Premium Monthly", "active") in overview.plans
        assert (unnamed_id, "1 месяц", "draft") in overview.plans
        assert len(overview.plans) == 2

        async with tenant_session_scope(
            sf, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            assert (await session.scalar(
                select(Plan).where(Plan.id == plan_id)
            )).status == "active"
            assert len((await session.scalars(
                select(PlanPrice).where(PlanPrice.plan_id == plan_id)
            )).all()) == 1

        other_actor = await setup.ensure_telegram_actor(
            sf, telegram_id=_new_telegram_id(),
            username="other_owner", first_name="Other",
        )
        async with tenant_session_scope(
            sf, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            session.add(ProjectMember(
                project_id=project_id, user_id=other_actor, role="viewer"
            ))
        # RLS visibility as a viewer must not be interpreted as management access.
        assert (await setup.owner_home(sf, actor_id=other_actor)).projects == ()
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await setup.setup_overview(
                sf, actor_id=other_actor, project_id=project_id
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await setup.choose_project(
                sf, actor_id=other_actor, project_id=project_id
            )
    finally:
        await engine.dispose()
