"""Regression tests for secure native paid channel invitations."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import native_channel_stars as native
from telegram_membership_saas.modules.telegram_commerce import handlers as buyer


async def test_project_ban_hides_paid_invite_url_and_all_checkout_options(
    monkeypatch,
):
    """Banned buyers cannot bypass bot checkout using a channel Stars URL."""
    session = SimpleNamespace(scalar=AsyncMock(return_value=True))

    @asynccontextmanager
    async def scoped(_factory, _project_id):
        yield session

    monkeypatch.setattr(buyer, "system_project_session_scope", scoped)
    catalog = AsyncMock(side_effect=AssertionError("banned catalog must not load"))
    monkeypatch.setattr(buyer, "list_buyer_offers", catalog)
    bot = MagicMock()
    bot.id = 5511
    bot.send_message = AsyncMock()
    chat_id, project_id = 778899, uuid4()
    result = await buyer._send_catalog(
        bot, chat_id=chat_id, session_factory=MagicMock(),
        project_id=project_id, price_id=uuid4(),
    )
    assert result is True
    catalog.assert_not_awaited()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Новые покупки заблокированы" in text
    buttons = [
        button for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for button in row
    ]
    assert not any(button.url and "t.me/+" in button.url for button in buttons)
    assert any(button.callback_data == "account:home" for button in buttons)
    session.scalar.assert_awaited_once()


async def test_archived_native_invite_deactivation_uses_original_snapshot(
    monkeypatch,
):
    """Archival or changed channel does not lose paid-link revocation."""
    original_chat = -100123456789
    link = "https://t.me/+original_paid_invite"
    price = SimpleNamespace(
        native_subscription_active=True,
        native_subscription_link=link,
        native_subscription_chat_id=str(original_chat),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[uuid4(), price]),
        flush=AsyncMock(),
    )

    @asynccontextmanager
    async def scoped(_factory, _ctx):
        yield session

    monkeypatch.setattr(native, "tenant_session_scope", scoped)
    monkeypatch.setattr(native, "checkout_retirement_lock", AsyncMock())
    monkeypatch.setattr(native, "_require_project_manager", AsyncMock())
    target = AsyncMock(side_effect=AssertionError(
        "deactivation must not require active plan or resource",
    ))
    monkeypatch.setattr(native, "_target", target)
    result = await native.deactivate_native_stars_link(
        MagicMock(), actor_id=uuid4(), project_id=uuid4(),
        plan_id=uuid4(), bot_id=5511,
    )
    assert result == (original_chat, link)
    assert price.native_subscription_active is False
    session.flush.assert_awaited_once()
    target.assert_not_awaited()
