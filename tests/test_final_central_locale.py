"""RU/EN owner retirement and legacy admin UI regression contracts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.obligation_locale import (
    obligation_error,
    obligation_message,
    obligation_status,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    OwnerObligations,
    OwnerPayment,
    OwnerSubscription,
)
from telegram_membership_saas.modules.telegram_admin import handlers as legacy_ui
from telegram_membership_saas.modules.telegram_admin.service import (
    AdminEntry,
    AdminProjectSnapshot,
)


def _legacy_snapshot():
    return AdminProjectSnapshot(
        project_name="Owner & Premium", timezone="Europe/Moscow",
        resources=(AdminEntry(name="Channel <A>", status="active"),),
        plans=(AdminEntry(name="VIP", status="draft"),),
        payment_methods=(AdminEntry(name="Bank", status="active"),),
        bots=(AdminEntry(name="my_premium_bot", status="registered"),),
    )


def test_legacy_admin_english_screen_and_stable_callback_ids():
    token = _language.set("en")
    try:
        text = legacy_ui._screen_text("main", _legacy_snapshot())
        assert "ViMi Pay · Management" in text
        assert "Owner &amp; Premium" in text
        assert "Connected bots: 1" in text
        assert "Timezone:" in text
        assert "Channel &lt;A&gt;" in legacy_ui._screen_text(
            "resources", _legacy_snapshot(),
        )
        assert "Active" in legacy_ui._screen_text("resources", _legacy_snapshot())
        buttons = [
            b for row in legacy_ui._keyboard("main", _legacy_snapshot()).inline_keyboard
            for b in row
        ]
        assert any(b.text == "📦 Plans" and b.callback_data == "adm:plans"
                   for b in buttons)
        assert any(b.callback_data == "lng:menu" for b in buttons)
        assert "adm:reviews" in [b.callback_data for b in buttons]
    finally:
        _language.reset(token)
    assert "Каналов и групп" in legacy_ui._screen_text("main", _legacy_snapshot())


async def test_legacy_bot_token_prompts_accept_both_languages_only_from_bot():
    rule = legacy_ui.BotTokenReplyFilter()
    bot = SimpleNamespace(id=700)
    for prefix in (legacy_ui.CONNECT_PROMPT_PREFIX,
                   legacy_ui.CONNECT_PROMPT_PREFIX_EN):
        msg = SimpleNamespace(
            text="fake-token-data",
            chat=SimpleNamespace(type="private"),
            from_user=SimpleNamespace(id=111),
            reply_to_message=SimpleNamespace(
                text=prefix + "\n\nDetails",
                from_user=SimpleNamespace(id=700),
            ),
        )
        assert await rule(msg, bot) is True
        msg.reply_to_message.from_user.id = 799
        assert await rule(msg, bot) is False


def test_retirement_translations_keep_customer_notice_meaning():
    original = (
        "Владелец завершил подписку. Оплаченный срок мог не закончиться; "
        "по вопросам возврата свяжитесь с владельцем."
    )
    assert obligation_message(original) == original
    token = _language.set("en")
    try:
        assert "paid term" in obligation_message(original)
        assert "Contact the owner" in obligation_message(original)
        assert "not found" in obligation_error("Подписка не найдена в этом проекте")
        assert obligation_status("waiting_review") == "Pending review"
    finally:
        _language.reset(token)


async def test_english_retirement_inbox_preserves_money_and_actions(monkeypatch):
    project_id, actor_id = uuid4(), uuid4()
    snapshot = OwnerObligations(
        blockers=RetirementBlockers(requests=1, subscriptions=1),
        payments=(OwnerPayment(
            id=uuid4(), telegram_user_id=301, username="buyer",
            status="waiting_review", amount="19.99", currency="EUR",
        ),),
        subscriptions=(OwnerSubscription(
            id=uuid4(), telegram_user_id=301, username="buyer",
            plan_name="Premium & VIP", period_end=None,
        ),),
        grants=(), access=(),
    )
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        owner_ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    monkeypatch.setattr(
        owner_ui, "list_owner_obligations", AsyncMock(return_value=snapshot),
    )
    router = owner_ui.create_owner_setup_router(MagicMock())
    handler = next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.id = 99
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        data="own:oblist",
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(id=9999, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )
    token = _language.set("en")
    try:
        await handler(query, bot)
        call = bot.send_message.await_args
        content = call.args[1]
        assert "Outstanding obligations before removal" in content
        assert "Payment requests: 1" in content
        assert "19.99 EUR" in content
        assert "Pending review" in content
        assert "Premium &amp; VIP" in content
        assert "Do not remove paid access" in content
        buttons = [
            b for row in call.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert any(b.text.startswith("💳 Reject request") and
                   b.callback_data.startswith("own:ob:pay:") for b in buttons)
        assert any(b.text.startswith("📦 End subscription") and
                   b.callback_data.startswith("own:ob:sub:") for b in buttons)
        assert any(b.callback_data == "own:oblist" for b in buttons)
        query.answer.assert_awaited_once()
    finally:
        _language.reset(token)
