"""Rich connection registry is a compact selector, never an account dashboard."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.connection_registry_ui import (
    connection_registry_classic,
    connection_registry_rich,
)
from telegram_membership_saas.modules.owner_setup.service import OwnerHome
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


@pytest.mark.parametrize("count,ru_word", [
    (0, "ботов"), (1, "бот"), (2, "бота"), (4, "бота"),
    (5, "ботов"), (11, "ботов"), (12, "ботов"),
    (13, "ботов"), (21, "бот"), (22, "бота"),
    (25, "ботов"), (100, "ботов"),
])
def test_russian_bot_count_is_grammatical_and_exact(count, ru_word):
    text = connection_registry_classic(
        total=count, page=0, total_pages=1,
    )
    if count:
        assert f"{count} {ru_word}" in text
        assert "Выберите подключение для управления." in text
    else:
        assert "пока нет подключённых ботов" in text
    assert "Счёт аккаунта" not in text
    assert "Счета ViMi Pay" not in text
    assert "ПРОВЕРКА" not in text


@pytest.mark.parametrize("lang,heading,count_phrase,page_label", [
    ("ru", "МОИ ПОДКЛЮЧЕНИЯ", "12 ботов", "Страница 2/9"),
    ("en", "MY CONNECTIONS", "12 connected bots", "Page 2/9"),
])
def test_compact_rich_and_classic_ru_en(lang, heading, count_phrase, page_label):
    token = _language.set(lang)
    try:
        rich = connection_registry_rich(total=12, page=1, total_pages=9)
        classic = connection_registry_classic(total=12, page=1, total_pages=9)
    finally:
        _language.reset(token)
    for body in (rich, classic):
        assert heading in body
        assert count_phrase in body
        assert page_label in body
        assert "Account bill" not in body
        assert "Счёт аккаунта" not in body
    assert RICH_ACTIONS_MARKER in rich
    assert "<h3>" in rich and "<p>" in rich
    assert "<h3>" not in classic and "<p>" not in classic
    assert "<b>" in classic and "<i>" in classic


def test_archived_screen_retains_own_count_and_back_navigation_copy():
    rich = connection_registry_rich(
        total=13, page=1, total_pages=2, archived=True,
    )
    classic = connection_registry_classic(
        total=13, page=1, total_pages=2, archived=True,
    )
    assert "АРХИВ ПОДКЛЮЧЕНИЙ" in rich
    assert "13 подключений" in rich
    assert "Страница 2/2" in classic
    assert "Счёт аккаунта" not in rich


def _bot():
    bot = MagicMock()
    bot.id = 1001001
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=101),
    )
    return bot


def _query(action: str):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"), message_id=100,
        ),
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        answer=AsyncMock(),
    )


def _cb(monkeypatch):
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    router = ui.create_owner_setup_router(MagicMock())
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


@pytest.mark.parametrize("count,page,wanted", [
    (0, 0, 0), (1, 0, 1),
    (12, 0, 12), (13, 0, 12), (13, 1, 1),
    (24, 1, 12), (25, 2, 1), (100, 8, 4),
])
async def test_live_registry_pages_are_exact_and_never_show_account_bill(
    monkeypatch, count, page, wanted,
):
    projects = tuple((uuid4(), f"@demo_{i}") for i in range(count))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome(projects, None)),
    )
    monkeypatch.setattr(
        ui, "list_archived_owner_connections", AsyncMock(return_value=()),
    )
    callback = _cb(monkeypatch)
    bot = _bot()
    q = _query("projects:" + str(page))
    await callback(q, bot)
    q.answer.assert_awaited_once()
    sent = bot.send_message.await_args
    markup = sent.kwargs["reply_markup"].inline_keyboard
    listed = [
        button for row in markup for button in row
        if (button.callback_data or "").startswith("own:select:")
    ]
    assert len(listed) == wanted
    assert [b.style for b in listed] == ["primary"] * wanted
    assert all(len(b.callback_data.encode()) <= 64 for b in listed)
    assert "Счёт аккаунта" not in sent.args[1]
    assert "own:saasinvoices:0" not in [
        b.callback_data for row in markup for b in row
    ]
    final_rows = [
        [b.callback_data for b in row]
        for row in markup
    ]
    assert ["own:newmanaged", "own:new"] in final_rows
    assert ["own:onboardhome"] in final_rows
    if count > 12:
        assert f"Страница {page + 1}/{(count + 11) // 12}" in sent.args[1]
    if count == 0:
        assert "Добавьте первого" in sent.args[1]
    elif count == 1:
        assert "1 бот" in sent.args[1]
    elif count == 13:
        assert "13 ботов" in sent.args[1]


async def test_archived_connections_have_same_12_per_page_and_no_billing(
    monkeypatch,
):
    archive = tuple((uuid4(), f"Archived {i}") for i in range(29))
    monkeypatch.setattr(
        ui, "list_archived_owner_connections",
        AsyncMock(return_value=archive),
    )
    cb = _cb(monkeypatch)
    bot = _bot()
    await cb(_query("archives:1"), bot)
    sent = bot.send_message.await_args
    markup = sent.kwargs["reply_markup"].inline_keyboard
    choices = [
        b for row in markup for b in row
        if (b.callback_data or "").startswith("own:archview:")
    ]
    assert len(choices) == 12
    assert "📄 Страница 2/3" in sent.args[1]
    assert "Счёт аккаунта" not in sent.args[1]
    action_ids = [
        b.callback_data for row in markup for b in row
    ]
    assert "own:archives:0" in action_ids
    assert "own:archives:2" in action_ids
    assert "own:projects" in action_ids
    assert "own:onboardhome" in action_ids
    assert "own:new" not in action_ids
    assert "own:saasinvoices:0" not in action_ids


async def test_registry_page_is_clamped_to_last_valid_page(monkeypatch):
    projects = tuple((uuid4(), f"Bot {i}") for i in range(13))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome(projects, None)),
    )
    monkeypatch.setattr(
        ui, "list_archived_owner_connections",
        AsyncMock(return_value=()),
    )
    cb = _cb(monkeypatch)
    bot = _bot()
    await cb(_query("projects:99999"), bot)
    sent = bot.send_message.await_args
    labels = [
        b.text for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "📄 2/2" in labels
    assert len([
        b for row in sent.kwargs["reply_markup"].inline_keyboard for b in row
        if (b.callback_data or "").startswith("own:select:")
    ]) == 1
