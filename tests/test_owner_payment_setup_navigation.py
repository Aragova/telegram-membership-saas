"""Approved RU/EN stage 3 lessons, real pagination, and single-card navigation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview


def _query(action: str, *, message_id: int = 701):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(type="private", id=345),
        ),
        answer=AsyncMock(),
    )


def _bot():
    bot = MagicMock()
    bot.id = 987
    bot.delete_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def _screen(monkeypatch, project, *, n_resources=1):
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project, project_name="ViMi Pay", timezone="Europe/Moscow",
        bots=(("vimitest21bot", "registered"),),
        resources=tuple(
            (uuid4(), "Connected channel", "active") for _ in range(n_resources)
        ),
        methods=(), plans=(),
        bot_instances=((uuid4(), "vimitest21bot", "registered"),),
    )))
    monkeypatch.setattr(
        ui, "saas_price_profile",
        AsyncMock(return_value={"currency_selected": False}),
    )


def _callback(project):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


def _callbacks(render):
    return [
        button.callback_data
        for row in render["reply_markup"].inline_keyboard
        for button in row
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_stage_three_intro_replaces_previous_card_and_is_blue(monkeypatch, lang):
    project = uuid4()
    _screen(monkeypatch, project)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("setupstep3"), _bot())
    finally:
        _language.reset(token)
    edited.assert_awaited_once()
    card = edited.await_args.kwargs
    assert card["message_id"] == 701
    assert "<b>3/4</b>" in card["rich_html"]
    assert "vimitest21bot" not in card["rich_html"]
    assert "Europe/Moscow" not in card["rich_html"]
    assert "vimitest21bot" not in card["classic_html"]
    assert _callbacks(card) == ["own:methods:guided"]
    assert card["reply_markup"].inline_keyboard[0][0].style == "primary"


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_first_method_screen_is_guided_blue_and_has_no_fake_page(monkeypatch, lang):
    project = uuid4()
    _screen(monkeypatch, project)
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(return_value=[]))
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("methods:guided"), _bot())
    finally:
        _language.reset(token)
    edited.assert_awaited_once()
    card = edited.await_args.kwargs
    assert card["message_id"] == 701
    assert "3/4" in card["rich_html"]
    assert ("ЧТО НУЖНО СДЕЛАТЬ" if lang == "ru" else "WHAT TO DO") in card["rich_html"]
    assert ("Страница 1" if lang == "ru" else "Page 1") not in card["rich_html"]
    assert _callbacks(card) == [
        "own:starspage:0", "own:cryptopay",
        "own:newmethod", "own:setupback2",
    ]
    assert all(
        button.style == "primary"
        for row in card["reply_markup"].inline_keyboard
        for button in row
    )
    assert not any("администратор" in row[0].text.lower()
                   for row in card["reply_markup"].inline_keyboard)


async def test_actual_manual_method_pagination_only_when_next_page_exists(monkeypatch):
    project = uuid4()
    _screen(monkeypatch, project)
    records = [
        SimpleNamespace(
            id=uuid4(), name=f"Method {i}", status="active", currencies=("RUB",),
        )
        for i in range(ui.METHOD_PAGE_SIZE + 1)
    ]
    async def list_page(_factory, *, page=0, **_kwargs):
        size = ui.METHOD_PAGE_SIZE
        return records[page * size:(page + 1) * size]
    monkeypatch.setattr(ui, "list_owner_methods", list_page)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    await _callback(project)(_query("methods:guided"), _bot())
    first = edited.await_args.kwargs
    assert "Страница 1" in first["rich_html"]
    assert "own:methodwizardpage:1" in _callbacks(first)
    edited.reset_mock()
    await _callback(project)(_query("methodwizardpage:1"), _bot())
    second = edited.await_args.kwargs
    assert "Страница 2" in second["rich_html"]
    assert "own:methodwizardpage:0" in _callbacks(second)
    assert "own:methodwizardpage:2" not in _callbacks(second)
    assert "own:setupback2" in _callbacks(second)
    records.pop()
    edited.reset_mock()
    await _callback(project)(_query("methods:guided"), _bot())
    assert "Страница 1" not in edited.await_args.kwargs["rich_html"]
    assert not any(s.startswith("own:methodwizardpage:")
                   for s in _callbacks(edited.await_args.kwargs))


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_back_replaces_methods_with_stage_two_not_admin_panel(monkeypatch, lang):
    project = uuid4()
    _screen(monkeypatch, project)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("setupback2"), _bot())
    finally:
        _language.reset(token)
    card = edited.await_args.kwargs
    assert card["message_id"] == 701
    assert "2/4" in card["rich_html"]
    assert _callbacks(card) == ["own:resources", "own:setupstep3"]
    assert "Панель администратора" not in card["rich_html"]


async def test_selecting_crypto_retires_the_previous_chooser(monkeypatch):
    project = uuid4()
    _screen(monkeypatch, project)
    monkeypatch.setattr(ui, "read_crypto_pay_connection", AsyncMock(return_value=None))
    monkeypatch.setattr(ui, "send_owner_rich", AsyncMock())
    bot = _bot()
    await _callback(project)(_query("cryptopay", message_id=777), bot)
    ui.read_crypto_pay_connection.assert_awaited_once()
    bot.delete_message.assert_awaited_once_with(345, 777)



async def test_new_manual_method_reveals_forward_to_stage_four(monkeypatch):
    project = uuid4()
    _screen(monkeypatch, project)
    manual = SimpleNamespace(
        id=uuid4(), name="Bank transfer", status="active", currencies=("RUB",),
    )
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(return_value=[manual]))
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    await _callback(project)(_query("methods:guided"), _bot())
    card = edited.await_args.kwargs
    callbacks = _callbacks(card)
    assert "own:plans" in callbacks
    assert callbacks.index("own:plans") < callbacks.index("own:setupback2")
    assert "own:setupback2" in callbacks
    assert "Страница 1" not in card["rich_html"]



@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_connected_crypto_pay_can_continue_to_plans_without_manual_method(
    monkeypatch, lang,
):
    project = uuid4()
    _screen(monkeypatch, project)
    monkeypatch.setattr(
        ui, "read_crypto_pay_connection",
        AsyncMock(return_value=SimpleNamespace(
            app_label="Owner app", environment="mainnet",
        )),
    )
    bot = _bot()
    token = _language.set(lang)
    try:
        await _callback(project)(_query("cryptopay"), bot)
    finally:
        _language.reset(token)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    buttons = [button for row in rows for button in row]
    forward = next(b for b in buttons if b.callback_data == "own:plans")
    assert forward.style == "primary"
    assert forward.text == (
        "➡️ Продолжить к тарифам" if lang == "ru" else "➡️ Continue to plans"
    )
    assert "own:methods" in [button.callback_data for button in buttons]
