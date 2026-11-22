"""Approved Crypto Pay three-screen onboarding, RU/EN and protected navigation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.crypto_pay_owner_ui import (
    crypto_network_classic,
    crypto_network_rich,
    crypto_overview_classic,
    crypto_overview_rich,
    crypto_token_classic,
    crypto_token_rich,
)
from telegram_membership_saas.modules.owner_setup.service import SetupFlow


def _bot():
    bot = MagicMock()
    bot.id = 987
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _query(action, message_id=701):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(type="private", id=345),
        ),
        answer=AsyncMock(),
    )


def _callback(monkeypatch, project):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "read_crypto_pay_connection", AsyncMock(return_value=None))
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


def _buttons(kwargs):
    return [
        button for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_all_three_screens_have_localized_rich_and_classic_copy(lang):
    token = _language.set(lang)
    try:
        for rendered in (
            crypto_overview_rich(None), crypto_overview_classic(None),
            crypto_network_rich(), crypto_network_classic(),
            crypto_token_rich("mainnet"), crypto_token_classic("mainnet"),
        ):
            assert "<b>" in rendered
            assert "@CryptoBot" in rendered
        overview = crypto_overview_rich(None)
        network = crypto_network_rich()
        token_card = crypto_token_rich("testnet")
        assert ("КАК ПОДКЛЮЧИТЬ" if lang == "ru" else "HOW TO CONNECT") in overview
        assert ("ТЕСТОВЫЙ РЕЖИМ" if lang == "ru" else "TEST MODE") in network
        assert "@CryptoTestnetBot" in token_card
        assert ("ПРЯМО СЮДА" if lang == "ru" else "RIGHT HERE") in token_card
        assert ("БЕЗОПАСНОСТЬ" if lang == "ru" else "SECURITY") in token_card
        assert "В меню" not in token_card
        assert "Main menu" not in token_card
        connected = crypto_overview_classic(SimpleNamespace(
            app_label="<Unsafe & label>", environment="testnet",
        ))
        assert "&lt;Unsafe &amp; label&gt;" in connected
        assert "<Unsafe & label>" not in connected
    finally:
        _language.reset(token)


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_environment_screen_explains_both_buttons_and_replaces_overview(
    monkeypatch, lang,
):
    project = uuid4()
    callback = _callback(monkeypatch, project)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    bot = _bot()
    token = _language.set(lang)
    try:
        await callback(_query("cryptonet", message_id=701), bot)
    finally:
        _language.reset(token)
    edited.assert_awaited_once()
    card = edited.await_args.kwargs
    assert card["message_id"] == 701
    assert "@CryptoTestnetBot" in card["rich_html"]
    assert ("реальной криптовалютой" if lang == "ru"
            else "real cryptocurrency") in card["rich_html"]
    buttons = _buttons(card)
    assert [button.callback_data for button in buttons] == [
        "own:cryptonet:mainnet", "own:cryptonet:testnet", "own:cryptopay",
    ]
    assert [button.style for button in buttons] == [
        "success", "primary", "primary",
    ]
    bot.delete_message.assert_not_awaited()


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("env,provider", [
    ("mainnet", "@CryptoBot"),
    ("testnet", "@CryptoTestnetBot"),
])
async def test_token_prompt_is_bound_and_has_only_back_button(
    monkeypatch, lang, env, provider,
):
    project = uuid4()
    callback = _callback(monkeypatch, project)
    set_flow = AsyncMock(return_value="generation-1")
    bind = AsyncMock()
    renderer = AsyncMock(return_value=SimpleNamespace(message_id=812))
    monkeypatch.setattr(ui, "set_flow", set_flow)
    monkeypatch.setattr(ui, "bind_prompt", bind)
    monkeypatch.setattr(ui, "send_owner_rich", renderer)
    bot = _bot()
    token = _language.set(lang)
    try:
        await callback(_query("cryptonet:" + env, message_id=777), bot)
    finally:
        _language.reset(token)
    assert set_flow.await_args.kwargs["payload"] == {"environment": env}
    assert set_flow.await_args.kwargs["bot_scope_id"] == bot.id
    assert bind.await_args.kwargs["message_id"] == 812
    assert bind.await_args.kwargs["generation"] == "generation-1"
    card = renderer.await_args.kwargs
    assert provider in card["rich_html"]
    assert provider in card["classic_html"]
    assert "В меню" not in card["classic_html"]
    assert "Main menu" not in card["classic_html"]
    buttons = _buttons(card)
    assert len(buttons) == 1
    assert buttons[0].callback_data == "own:promptback:generation-1"
    assert buttons[0].style == "primary"
    bot.delete_message.assert_awaited_once_with(345, 777)


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_back_from_token_returns_to_network_then_retires_old_prompt(
    monkeypatch, lang,
):
    project = uuid4()
    callback = _callback(monkeypatch, project)
    monkeypatch.setattr(ui, "back_owner_flow", AsyncMock(return_value=SetupFlow(
        "crypto_pay_token", project, {"environment": "testnet"}, 812,
        generation="generation-1",
    )))
    monkeypatch.setattr(ui, "cancel_draft", AsyncMock())
    rendered = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", rendered)
    bot = _bot()
    token = _language.set(lang)
    try:
        await callback(_query("promptback:generation-1", message_id=812), bot)
    finally:
        _language.reset(token)
    rendered.assert_awaited_once()
    assert [b.callback_data for b in _buttons(rendered.await_args.kwargs)] == [
        "own:cryptonet:mainnet", "own:cryptonet:testnet", "own:cryptopay",
    ]
    bot.delete_message.assert_awaited_once_with(345, 812)


async def test_stale_token_back_does_not_delete_current_wizard(monkeypatch):
    project = uuid4()
    callback = _callback(monkeypatch, project)
    monkeypatch.setattr(ui, "back_owner_flow", AsyncMock(return_value=None))
    bot = _bot()
    await callback(_query("promptback:old-generation", message_id=812), bot)
    bot.delete_message.assert_not_awaited()
    assert not bot.send_message.await_count
