"""Regression: bank instructions cannot be overwritten by stale editor state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aiogram.types import CallbackQuery, Message

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui


def _message(text: str, uid: int = 7777) -> Message:
    return Message.model_construct(
        from_user=SimpleNamespace(
            id=uid, username="owner", first_name="Owner",
        ), text=text,
        chat=SimpleNamespace(type="private", id=uid),
    )


def _callback(data: str, uid: int = 7777) -> CallbackQuery:
    return CallbackQuery.model_construct(
        from_user=SimpleNamespace(id=uid, username="owner", first_name="Owner"),
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=uid)),
    )


async def test_global_navigation_clears_bank_editor_for_owner_language_and_platform():
    navigation = ui.PlatformBankNavigationMiddleware()
    called = AsyncMock()
    for callback in ("own:projects", "lng:menu", "plat:billing:0", "plat:home"):
        navigation.pending.add(7777)
        await navigation(called, _callback(callback), {})
        assert 7777 not in navigation.pending
    assert called.await_count == 4


async def test_global_commands_cancel_pending_bank_entry_but_cancel_stays_available():
    navigation = ui.PlatformBankNavigationMiddleware()
    called = AsyncMock()
    for command in (
        "/billing", "/platform", "/start", "/menu", "/help",
        "/billing please", "  /billing",
    ):
        navigation.pending.add(7777)
        await navigation(called, _message(command), {})
        assert 7777 not in navigation.pending
    for command in ("/cancel", "/cancel@vimipay_bot", "/cancel please"):
        navigation.pending.add(7777)
        await navigation(called, _message(command), {})
        assert 7777 in navigation.pending
    assert called.await_count == 10


async def test_editor_filter_rejects_unrelated_commands_while_pending():
    navigation = ui.PlatformBankNavigationMiddleware()
    navigation.pending.add(7777)
    guard = ui._PlatformBankEntryFilter(navigation.pending)
    assert not await guard(_message("/billing"))
    assert not await guard(_message("  /billing please"))
    assert not await guard(_message("/platform"))
    assert not await guard(_message("/start"))
    assert await guard(_message("/cancel"))
    assert await guard(_message("/cancel@vimipay_bot"))
    assert await guard(_message("/cancel please"))
    assert await guard(_message("Bank recipient, account 12345"))
    assert not await guard(_message("Bank recipient, account 12345", uid=8888))


async def test_platform_navigation_leaves_bank_editor_even_without_dispatcher(monkeypatch):
    navigation = ui.PlatformBankNavigationMiddleware()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        ui, "platform_saas_payment_details",
        AsyncMock(return_value={
            "configured": False, "instructions": None,
            "billing_enabled": False, "trial_enabled": False,
            "holds_enabled": False,
        }),
    )
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
        bank_navigation=navigation,
    )
    handler = next(
        record.callback for record in router.callback_query.handlers
        if record.callback.__name__ == "platform_callback"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    # aiogram CallbackQuery is a frozen Pydantic model; mock the method on
    # the class rather than trying to assign it on an instance.
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    edit = _callback("plat:bankedit")
    await handler(edit, bot)
    assert 7777 in navigation.pending
    navigate = _callback("plat:bankdetails")
    await handler(navigate, bot)
    assert 7777 not in navigation.pending
    assert answer.await_count == 2


async def test_editor_accepts_cancel_forms_but_never_writes_bank_details(monkeypatch):
    navigation = ui.PlatformBankNavigationMiddleware()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    save = AsyncMock()
    monkeypatch.setattr(ui, "configure_platform_saas_payment", save)
    monkeypatch.setattr(
        ui, "platform_saas_payment_details",
        AsyncMock(return_value={
            "configured": False, "instructions": None,
            "billing_enabled": False, "trial_enabled": False,
            "holds_enabled": False,
        }),
    )
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
        bank_navigation=navigation,
    )
    bank_handler = next(
        entry.callback for entry in router.message.handlers
        if entry.callback.__name__ == "platform_bank_entry"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    for command in ("/cancel", "/cancel@vimipay_bot", "/cancel please"):
        navigation.pending.add(7777)
        message = _message(command)
        await bank_handler(message, bot)
        assert 7777 not in navigation.pending
    for command in ("/billing", " /billing please", "/platform"):
        navigation.pending.add(7777)
        await bank_handler(_message(command), bot)
        assert 7777 not in navigation.pending
    save.assert_not_awaited()


