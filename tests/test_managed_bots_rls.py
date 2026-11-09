"""PostgreSQL/RLS integration: managed creation uses existing atomic bot registrar."""
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
from telegram_membership_saas.modules.bots import service as bot_service
from telegram_membership_saas.modules.bots.service import (
    VerifiedBot,
    decrypt_token,
)
from telegram_membership_saas.modules.owner_setup import managed_bots as managed
from telegram_membership_saas.modules.owner_setup import service as owner_service
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
    owner_home,
    set_flow,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL tenant RLS",
)


def _id():
    return uuid4().int % 8_000_000_000_000_000_000 + 1


async def test_managed_creation_is_atomic_one_owner_one_bot_then_explicit_sync(
    monkeypatch,
):
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    owner_tg, stranger_tg, bot_tg = _id(), _id(), _id()
    key = SecretStr(Fernet.generate_key().decode())
    token1 = str(bot_tg) + ":" + "A" * 35
    token2 = str(bot_tg) + ":" + "B" * 35
    manager = SimpleNamespace(
        id=_id(), get_managed_bot_token=AsyncMock(return_value=token1),
    )
    async def fake_verified(token):
        if token not in (token1, token2):
            raise ValueError("Token rejected")
        return VerifiedBot(
            telegram_bot_id=bot_tg,
            username="generated_managed_bot",
            display_name="Managed Bot",
        )
    monkeypatch.setattr(owner_service, "verify_telegram_bot", fake_verified)
    monkeypatch.setattr(bot_service, "verify_telegram_bot", fake_verified)
    monkeypatch.setattr(managed, "verify_telegram_bot", fake_verified)
    try:
        owner_id = await ensure_telegram_actor(
            factory, telegram_id=owner_tg,
            username="managed_owner", first_name="Owner",
        )
        stranger_id = await ensure_telegram_actor(
            factory, telegram_id=stranger_tg,
            username="unrelated", first_name="Another user",
        )
        # Telegram events without the owner's explicit 15-minute request
        # must neither access managed tokens nor create a project.
        ignored = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=owner_tg,
            managed_bot_id=bot_tg, encryption_key=key, generation="old",
        )
        assert ignored.kind == "ignored"
        manager.get_managed_bot_token.assert_not_awaited()

        await set_flow(
            factory, actor_id=owner_id, step="managed_bot_create",
            payload={"request_id": 1},
        )
        bad = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=stranger_tg,
            managed_bot_id=bot_tg, encryption_key=key, generation="old",
        )
        assert bad.kind == "ignored"
        manager.get_managed_bot_token.assert_not_awaited()

        pending_generation = await managed.stage_managed_bot_candidate(
            factory, actor_id=owner_id, managed_bot_id=bot_tg,
        )
        assert pending_generation is not None
        # A second event for a different bot cannot overwrite this candidate.
        assert await managed.stage_managed_bot_candidate(
            factory, actor_id=owner_id, managed_bot_id=bot_tg + 1,
        ) is None
        # Wrong generation cannot access a token or consume the current request.
        stale = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=owner_tg,
            managed_bot_id=bot_tg, encryption_key=key, generation="old",
        )
        assert stale.kind == "ignored"
        manager.get_managed_bot_token.assert_not_awaited()
        result = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=owner_tg,
            managed_bot_id=bot_tg, encryption_key=key,
            generation=pending_generation,
        )
        assert result.kind == "created"
        assert result.actor_id == owner_id
        assert result.username == "generated_managed_bot"
        assert result.project_id is not None
        projects = (await owner_home(factory, actor_id=owner_id)).projects
        assert len(projects) == 1
        assert projects[0] == (result.project_id, "@generated_managed_bot")
        assert (await owner_home(factory, actor_id=stranger_id)).projects == ()
        # Receiving both updateManagedBot and managed_bot_created is harmless.
        duplicate = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=owner_tg,
            managed_bot_id=bot_tg, encryption_key=key,
            generation=pending_generation,
        )
        assert duplicate.kind == "ignored"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=result.project_id),
        ) as session:
            row = await session.scalar(select(BotInstance).where(
                BotInstance.project_id == result.project_id,
            ))
            assert row is not None and row.telegram_bot_id == bot_tg
            assert token1 not in row.token_ciphertext
            assert decrypt_token(row.token_ciphertext, key=key) == token1

        manager.get_managed_bot_token.return_value = token2
        # Token updates do not silently change the live bot when Telegram
        # emits unrelated managed_bot updates; owner must tap Sync.
        unsolicited = await managed.connect_managed_bot_event(
            factory, central_bot=manager, creator_telegram_id=owner_tg,
            managed_bot_id=bot_tg, encryption_key=key,
            generation=pending_generation,
        )
        assert unsolicited.kind == "ignored"

        refreshed = await managed.sync_managed_bot_token(
            factory, central_bot=manager, actor_id=owner_id,
            actor_telegram_id=owner_tg, managed_bot_id=bot_tg,
            encryption_key=key,
        )
        assert refreshed.kind == "refreshed"
        assert refreshed.project_id == result.project_id
        no_change = await managed.sync_managed_bot_token(
            factory, central_bot=manager, actor_id=owner_id,
            actor_telegram_id=owner_tg, managed_bot_id=bot_tg,
            encryption_key=key,
        )
        assert no_change.kind == "unchanged"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=result.project_id),
        ) as session:
            row = await session.scalar(select(BotInstance).where(
                BotInstance.project_id == result.project_id,
            ))
            assert decrypt_token(row.token_ciphertext, key=key) == token2

        with pytest.raises(PermissionError):
            await managed.sync_managed_bot_token(
                factory, central_bot=manager, actor_id=stranger_id,
                actor_telegram_id=stranger_tg, managed_bot_id=bot_tg,
                encryption_key=key,
            )
        with pytest.raises(PermissionError):
            await managed.sync_managed_bot_token(
                factory, central_bot=manager, actor_id=owner_id,
                actor_telegram_id=stranger_tg, managed_bot_id=bot_tg,
                encryption_key=key,
            )
        assert await managed.managed_bot_id_for_project(
            factory, actor_id=owner_id, project_id=result.project_id,
        ) == bot_tg
        with pytest.raises(PermissionError):
            await managed.managed_bot_id_for_project(
                factory, actor_id=stranger_id, project_id=result.project_id,
            )
    finally:
        await engine.dispose()


async def test_managed_identity_mismatch_cannot_consume_pending_request(monkeypatch):
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    owner_tg, bot_tg = _id(), _id()
    key = SecretStr(Fernet.generate_key().decode())
    manager = SimpleNamespace(
        id=_id(), get_managed_bot_token=AsyncMock(
            return_value=str(bot_tg) + ":" + "A" * 35,
        ),
    )
    try:
        actor = await ensure_telegram_actor(
            factory, telegram_id=owner_tg,
            username="owner_mismatch", first_name="Owner",
        )
        await set_flow(factory, actor_id=actor, step="managed_bot_create")
        generation = await managed.stage_managed_bot_candidate(
            factory, actor_id=actor, managed_bot_id=bot_tg,
        )
        assert generation is not None
        monkeypatch.setattr(managed, "verify_telegram_bot", AsyncMock(
            return_value=VerifiedBot(
                telegram_bot_id=bot_tg + 1,
                username="another_bot", display_name="Different",
            ),
        ))
        with pytest.raises(ValueError, match="identity"):
            await managed.connect_managed_bot_event(
                factory, central_bot=manager, creator_telegram_id=owner_tg,
                managed_bot_id=bot_tg, encryption_key=key,
                generation=generation,
            )
        assert await managed.managed_creation_pending(factory, actor)
        assert (await owner_home(factory, actor_id=actor)).projects == ()
    finally:
        await engine.dispose()
