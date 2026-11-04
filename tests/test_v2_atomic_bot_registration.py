"""Token-first registration must commit exactly one bot + project or nothing."""
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.bots.service import VerifiedBot
from telegram_membership_saas.modules.owner_setup import service as owner_service
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
    owner_home,
    register_new_owner_bot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires PostgreSQL tenant RLS and migration 0018",
)


def _telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_failed_new_bot_token_never_creates_orphan_project(monkeypatch):
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    token = "123456:" + "A" * 35
    key = SecretStr(Fernet.generate_key().decode())
    owner_tg_id = _telegram_id()
    bot_tg_id = _telegram_id()
    try:
        actor = await ensure_telegram_actor(
            factory, telegram_id=owner_tg_id,
            username="atomic_owner", first_name="Owner",
        )
        before = (await owner_home(factory, actor_id=actor)).projects
        async def rejected(_token):
            raise ValueError("Telegram не подтвердил токен бота")
        monkeypatch.setattr(owner_service, "verify_telegram_bot", rejected)
        with pytest.raises(ValueError, match="не подтвердил"):
            await register_new_owner_bot(
                factory, actor_id=actor, actor_telegram_user_id=owner_tg_id,
                token=token, encryption_key=key, central_bot_id=999,
            )
        assert (await owner_home(factory, actor_id=actor)).projects == before

        async def verified(_token):
            return VerifiedBot(
                telegram_bot_id=bot_tg_id,
                username="test_connected_bot", display_name="Test",
            )
        monkeypatch.setattr(owner_service, "verify_telegram_bot", verified)
        project_id, registration = await register_new_owner_bot(
            factory, actor_id=actor, actor_telegram_user_id=owner_tg_id,
            token=token, encryption_key=key, central_bot_id=999,
        )
        assert registration.telegram_bot_id == bot_tg_id
        entries = (await owner_home(factory, actor_id=actor)).projects
        assert len(entries) == len(before) + 1
        assert (project_id, "@test_connected_bot") in entries

        with pytest.raises(ValueError, match="уже подключён"):
            await register_new_owner_bot(
                factory, actor_id=actor, actor_telegram_user_id=owner_tg_id,
                token=token, encryption_key=key, central_bot_id=999,
            )
        # The unique identity constraint rolls back the Project insert too.
        assert (await owner_home(factory, actor_id=actor)).projects == entries
    finally:
        await engine.dispose()
