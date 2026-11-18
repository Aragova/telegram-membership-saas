"""Regression tests for secure native paid channel invitations."""
from contextlib import asynccontextmanager
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers
from telegram_membership_saas.modules.crypto_pay import buyer as crypto_buyer
from telegram_membership_saas.modules.owner_setup import native_channel_stars as native
from telegram_membership_saas.modules.telegram_commerce import handlers as buyer
from telegram_membership_saas.modules.telegram_commerce import storefront
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption


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


def test_active_native_paid_link_is_bot_retirement_blocker():
    guard = RetirementBlockers(native_paid_links=1)
    assert guard.has_blockers
    assert "активных платных ссылок канала: 1" in guard.display()


async def test_native_activation_rejects_saas_hold_before_telegram_link_creation(
    monkeypatch,
):
    project_id, plan_id, bot_id = uuid4(), uuid4(), 5511
    plan = SimpleNamespace(duration_value=1, duration_unit="month")
    price = SimpleNamespace(
        amount=Decimal("100"), native_subscription_active=False,
    )
    session = SimpleNamespace(scalar=AsyncMock(
        side_effect=[uuid4(), plan, price],
    ))
    check = AsyncMock(return_value="held")
    monkeypatch.setattr(native, "native_commerce_state", check)
    with pytest.raises(PermissionError, match="заблокированы"):
        await native._target(
            session, project_id=project_id, plan_id=plan_id, bot_id=bot_id,
        )
    check.assert_awaited_once()
    assert session.scalar.await_count == 3


@pytest.mark.parametrize(("state", "visible"), [
    ("clear", True),
    ("disabled", True),
    ("held", False),
])
async def test_native_paid_checkout_is_not_published_under_hold(
    monkeypatch, state, visible,
):
    price_id, plan_id, project_id = uuid4(), uuid4(), uuid4()
    option = CheckoutOption(
        plan_id=plan_id, plan_name="Premium", duration_value=1,
        duration_unit="month", plan_price_id=price_id,
        currency_code="XTR", amount=Decimal("100"), currency_precision=0,
        payment_method_id=UUID(int=0), payment_method_name="⭐ Telegram Stars",
    )
    monkeypatch.setattr(storefront, "list_checkout_options", AsyncMock(
        return_value=(option,),
    ))
    monkeypatch.setattr(crypto_buyer, "subscription_crypto_options", AsyncMock(
        return_value=[],
    ))
    original = "https://t.me/+approved_channel_invite"
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(
            all=lambda: [price_id],
        )),
        scalar=AsyncMock(return_value=state in ("clear", "disabled")),
        execute=AsyncMock(side_effect=[
            SimpleNamespace(all=lambda: []),
            SimpleNamespace(all=lambda: [(
                price_id, original, "-100777", "-100777", "active", None,
            )] if state in ("clear", "disabled") else []),
        ]),
    )
    offers = await storefront.list_buyer_offers(
        session, project_id=project_id, bot_id=5511,
    )
    session.scalar.assert_awaited_once()
    assert "native_buyer_sales_allowed" in str(
        session.scalar.await_args.args[0],
    )
    if visible:
        assert len(offers) == 1
        assert offers[0].native_stars_link == original
    else:
        assert offers == ()
        assert not any(
            (offer.native_stars_link or "").startswith("https://t.me/+",)
            for offer in offers
        )


async def test_native_buyer_hold_rpc_is_authorized_without_admin_actor():
    """Real PostgreSQL runtime role has only a scoped boolean, not billing data."""
    url = os.getenv("TMS_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL runtime test database is not configured")
    engine = create_async_engine(url)
    project_id, other_project = uuid4(), uuid4()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with system_project_session_scope(factory, project_id) as session:
            statement = text(
                "SELECT public.platform_saas_native_buyer_sales_allowed("
                ":project,:bot)"
            )
            # The test project has no connected bot: fail closed, not 42501.
            allowed = await session.scalar(
                statement, {"project": project_id, "bot": 987654321},
            )
            assert allowed is False
            # A spoofed project argument cannot cross tenant context.
            other = await session.scalar(
                statement, {"project": other_project, "bot": 987654321},
            )
            assert other is False
    finally:
        await engine.dispose()
