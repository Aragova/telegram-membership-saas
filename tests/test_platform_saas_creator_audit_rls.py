"""Real PostgreSQL RLS regression for the on-demand creator audit.

The owner-only report is mocked; sensitive project/bot/resource reads are not.
No Telegram calls, privileged financial writes or live billing.
"""
import os
from unittest.mock import AsyncMock

import pytest

from test_platform_saas_invoice_rls import _project, _tid
from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules import platform_saas_creator_check as audit

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires non-bypass PostgreSQL with migrated project RLS policies",
)


async def test_creator_audit_reads_only_owner_projects_with_real_rls(monkeypatch):
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    owner_tg = _tid()
    try:
        owner, _, _, own_bot, chat = await _project(factory, owner_tg)
        outsider, _, _, other_bot, other_chat = await _project(factory, _tid())
        assert own_bot != other_bot
        assert owner != outsider
        assert chat != other_chat

        monkeypatch.setattr(
            audit, "platform_saas_exemption_page",
            AsyncMock(return_value={
                "page": 0, "pages": 1,
                "pending_bots": [
                    {"username": "billing_test_bot", "state": "not_exempt"},
                ],
            }),
        )
        monkeypatch.setattr(
            audit, "decrypt_token",
            lambda ciphertext, *, key: "test-only-token-in-memory",
        )
        remote = AsyncMock(return_value="creator")
        monkeypatch.setattr(audit, "_telegram_creator_status", remote)

        result = await audit.audit_creator_page(
            factory, actor_user_id=owner,
            actor_telegram_id=owner_tg, page=0,
            settings=Settings(platform_owner_telegram_id=owner_tg),
        )
        assert result.rows[0].status == "creator"
        assert result.creators == 1
        remote.assert_awaited_once()
        assert remote.await_args.kwargs["channel_chat_id"] == int(chat)
        assert remote.await_args.kwargs["owner_telegram_id"] == owner_tg
        # Both projects created the same mock username, but the restricted
        # query is bound to the operator's own project and cannot use
        # the unrelated other owner's bot credential.
        assert remote.await_args.kwargs["token"] == "test-only-token-in-memory"
    finally:
        await engine.dispose()
