"""Stars setup chooses the real recipient before asking for an amount."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_v2_tenant_onboarding_ui import (
    _auth,
    _bot,
    _callback,
    _query,
    _snapshot,
)

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupFlow


def _callbacks(markup):
    return [
        b.callback_data for row in markup.inline_keyboard for b in row
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_no_plan_overview_and_recipient_choice_before_price(monkeypatch, lang):
    project = uuid4()
    _auth(monkeypatch, project)
    current = _snapshot(project, has_channel=True)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=current))
    create = AsyncMock()
    monkeypatch.setattr(ui, "create_one_month_stars_plan", create)
    start = AsyncMock(return_value="fresh-generation")
    binding = AsyncMock()
    retire = AsyncMock()
    monkeypatch.setattr(ui, "set_flow", start)
    monkeypatch.setattr(ui, "bind_prompt", binding)
    monkeypatch.setattr(ui, "retire_crypto_owner_card", retire)
    bot = _bot()
    bot.send_message.return_value = SimpleNamespace(message_id=125)

    token = _language.set(lang)
    try:
        handler = _callback(project)
        await handler(_query("starspage:0"), bot)
        page = bot.send_message.await_args
        assert ("ВАШИ ТАРИФЫ" if lang == "ru" else "YOUR PLANS") in page.args[1]
        assert _callbacks(page.kwargs["reply_markup"]) == [
            "own:starsnew", "own:methods",
        ]
        await handler(_query("starsnew"), bot)
        choice = bot.send_message.await_args
        text = choice.args[1]
        assert ("Куда получать звёзды" if lang == "ru"
                else "Where should Stars go") in text
        assert "5%" not in text
        assert "комисси" not in text.lower()
        assert _callbacks(choice.kwargs["reply_markup"]) == [
            "own:starsmode:bot", "own:starsmode:channel",
            "own:starspage:0",
        ]
        assert start.await_count == 0
        create.assert_not_awaited()

        selection = _query("starsmode:bot")
        selection.message.message_id = 701
        await handler(selection, bot)
        pricing = bot.send_message.await_args
        assert ("250 000" if lang == "ru" else "250 000") in pricing.args[1]
        assert "own:promptback:fresh-generation" in _callbacks(
            pricing.kwargs["reply_markup"],
        )
        assert "own:promptcancel:fresh-generation" not in _callbacks(
            pricing.kwargs["reply_markup"],
        )
        assert start.await_args.kwargs["step"] == "stars_month_price"
        assert start.await_args.kwargs["payload"] == {
            "resource_id": current.resources[0][0].hex, "mode": "bot",
        }
        binding.assert_awaited_once()
        create.assert_not_awaited()
        retire.assert_awaited_once()
    finally:
        _language.reset(token)


async def test_native_selection_uses_eligible_channels_and_private_preflight(
    monkeypatch,
):
    project, first, second, group_id = uuid4(), uuid4(), uuid4(), uuid4()
    _auth(monkeypatch, project)
    snapshot = replace(
        _snapshot(project, has_channel=True),
        resources=((first, "Channel 1", "active"),
                   (second, "Channel 2", "active"),
                   (group_id, "Supergroup", "active")),
    )
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        ui, "list_native_stars_setup_channels",
        AsyncMock(return_value=((first, "Channel 1"),
                                (second, "Channel 2"))),
    )
    monkeypatch.setattr(
        ui, "native_stars_setup_chat", AsyncMock(return_value=-100123456),
    )
    begin = AsyncMock(return_value="gen-new")
    monkeypatch.setattr(ui, "set_flow", begin)
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    monkeypatch.setattr(ui, "retire_crypto_owner_card", AsyncMock())
    bot = _bot()
    bot.send_message.return_value = SimpleNamespace(message_id=125)
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(
        type="channel", username=None,
    ))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="administrator", can_invite_users=True,
    ))
    handler = _callback(project)

    await handler(_query("starsmode:channel"), bot)
    menu = bot.send_message.await_args
    callbacks = _callbacks(menu.kwargs["reply_markup"])
    assert callbacks == [
        "own:starsselect:channel:" + first.hex,
        "own:starsselect:channel:" + second.hex,
        "own:starsnew",
    ]
    assert not any(group_id.hex in call for call in callbacks)
    await handler(_query("starsselect:channel:" + first.hex), bot)
    price = bot.send_message.await_args
    assert "10 000" in price.args[1]
    assert "250 000" not in price.args[1]
    assert begin.await_args.kwargs["payload"] == {
        "resource_id": first.hex, "mode": "channel",
    }
    assert _callbacks(price.kwargs["reply_markup"]) == [
        "own:promptback:gen-new",
    ]


async def test_native_recipient_no_channel_has_explanation_not_dead_end(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_snapshot(project, has_channel=True)),
    )
    monkeypatch.setattr(
        ui, "list_native_stars_setup_channels",
        AsyncMock(return_value=()),
    )
    bot = _bot()
    await _callback(project)(_query("starsmode:channel"), bot)
    rendered = bot.send_message.await_args
    assert "приватный" in rendered.args[1].lower()
    assert _callbacks(rendered.kwargs["reply_markup"]) == ["own:starsnew"]


async def test_native_link_confirmation_creates_plan_and_no_duplicate_cards(
    monkeypatch,
):
    project, resource = uuid4(), uuid4()
    _auth(monkeypatch, project)
    # A saved native plan may be followed by an onboarding continuation;
    # this legacy test fixture has no registered bot and is not in setup.
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_snapshot(project, has_channel=True)),
    )
    monkeypatch.setattr(
        ui, "consume_button_flow", AsyncMock(return_value=SetupFlow(
            "stars_native_confirm", project,
            {"resource_id": resource.hex, "stars": 199, "mode": "channel"},
            400, generation="confirmed",
        )),
    )
    monkeypatch.setattr(
        ui, "native_stars_setup_chat", AsyncMock(return_value=-100123456),
    )
    save = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(ui, "create_one_month_stars_plan", save)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    bot = _bot()
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(
        type="channel", username=None,
    ))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="administrator", can_invite_users=True,
    ))
    bot.create_chat_subscription_invite_link = AsyncMock(
        return_value=SimpleNamespace(
            invite_link="https://t.me/+test_url",
            subscription_price=199, subscription_period=2592000,
        ),
    )
    bot.revoke_chat_invite_link = AsyncMock()
    query = _query("starsnativecreate:confirmed")
    query.message.message_id = 400
    await _callback(project)(query, bot)
    save.assert_awaited_once()
    assert save.await_args.kwargs["resource_id"] == resource
    assert save.await_args.kwargs["native_chat_id"] == -100123456
    assert save.await_args.kwargs["native_bot_id"] == bot.id
    assert save.await_args.kwargs["native_invite_link"] == "https://t.me/+test_url"
    edited.assert_awaited_once()
    assert "Платная ссылка создана" in edited.await_args.kwargs["rich_html"]
    bot.revoke_chat_invite_link.assert_not_awaited()


async def test_failed_native_plan_save_revokes_telegram_invite(monkeypatch):
    project, resource = uuid4(), uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(
        ui, "consume_button_flow", AsyncMock(return_value=SetupFlow(
            "stars_native_confirm", project,
            {"resource_id": resource.hex, "stars": 199, "mode": "channel"},
            400, generation="confirmed",
        )),
    )
    monkeypatch.setattr(
        ui, "native_stars_setup_chat", AsyncMock(return_value=-100123456),
    )
    monkeypatch.setattr(
        ui, "create_one_month_stars_plan",
        AsyncMock(side_effect=ValueError("Plan already exists")),
    )
    restore = AsyncMock()
    monkeypatch.setattr(ui, "restore_button_flow", restore)
    bot = _bot()
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(
        type="channel", username=None,
    ))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="administrator", can_invite_users=True,
    ))
    bot.create_chat_subscription_invite_link = AsyncMock(
        return_value=SimpleNamespace(
            invite_link="https://t.me/+test_url",
            subscription_price=199, subscription_period=2592000,
        ),
    )
    bot.revoke_chat_invite_link = AsyncMock()
    query = _query("starsnativecreate:confirmed")
    query.message.message_id = 400
    await _callback(project)(query, bot)
    restore.assert_awaited_once()
    assert restore.await_args.kwargs["source"].generation == "confirmed"
    bot.revoke_chat_invite_link.assert_awaited_once_with(
        -100123456, "https://t.me/+test_url",
    )
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_old_stars_button_cannot_skip_recipient_choice(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_snapshot(project, has_channel=True)),
    )
    bot = _bot()
    await _callback(project)(_query("starsnewok"), bot)
    assert "own:starsmode:channel" in _callbacks(
        bot.send_message.await_args.kwargs["reply_markup"],
    )


def test_older_native_plan_setup_progress_is_unchanged():
    project = uuid4()
    native = replace(
        _snapshot(project, has_channel=True),
        plans=((uuid4(), "1 месяц", "active"),),
        methods=(), sales_configured=True,
        live_bot_username="stars_channel_bot",
    )
    assert ui._next_setup_step(native) is None
    assert any(
        line.startswith("✅") and "Способ оплаты" in line
        for line in ui._progress(native)
    )



async def test_native_confirmation_callback_is_not_cancelled_by_middleware(
    monkeypatch,
):
    """Exercise actual dispatcher middleware, not only the direct callback."""
    project = uuid4()
    _auth(monkeypatch, project)
    cancel = AsyncMock()
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    middleware = ui.ClearOwnerInputOnNavigation(
        object(), fixed_project_id=project,
    )
    bot = _bot()
    called = AsyncMock()
    event = SimpleNamespace(
        data="own:starsnativecreate:confirmed",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=7777),
        ),
        from_user=SimpleNamespace(id=7777, username="owner", first_name="Owner"),
    )
    await middleware(called, event, {"bot": bot})
    called.assert_awaited_once()
    cancel.assert_not_awaited()
