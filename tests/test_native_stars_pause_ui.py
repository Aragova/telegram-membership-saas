"""Native paid-channel Stars: eligible buttons, safe pause and single-card UX."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.exceptions import AiogramError
from test_owner_payment_setup_navigation import (
    _bot,
    _callback,
    _callbacks,
    _query,
    _screen,
)

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.error_locale import owner_error
from telegram_membership_saas.modules.owner_setup.native_channel_stars import (
    NativeStarsTarget,
)
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.owner_setup.tenant_admin import PriceInfo


def _plan_info(
    *, duration_value=1, duration_unit="month",
    eligible=True, native=False, amount=200,
):
    return SimpleNamespace(
        duration_value=duration_value,
        duration_unit=duration_unit,
        native_channel_eligible=eligible,
        prices=(
            PriceInfo(
                currency="XTR", amount=Decimal(amount), precision=0,
                method_count=0, native_stars_active=native,
            ),
        ),
    )


def _prepare(monkeypatch, *, info, plans=None):
    project = uuid4()
    _screen(monkeypatch, project)
    if plans is None:
        plans = ((uuid4(), "1 месяц", "active"),)
    current = ui.setup_overview.return_value
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=SetupOverview(
            project_id=project, project_name="ViMi Pay",
            timezone=current.timezone, bots=current.bots,
            resources=current.resources, bot_instances=current.bot_instances,
            plans=plans, sales_configured=True, methods=(),
        ),
    ))
    get_info = AsyncMock(return_value=info)
    monkeypatch.setattr(ui, "get_owner_plan_info", get_info)
    edited = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", edited)
    return project, edited


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_channel_button_only_for_one_month_private_channel(
    monkeypatch, lang,
):
    one, forever, group, expensive = [uuid4() for _ in range(4)]
    project, edit = _prepare(
        monkeypatch,
        info=_plan_info(),
        plans=((one, "1 месяц", "active"),
               (forever, "Навсегда", "active"),
               (group, "Группа 1 месяц", "active"),
               (expensive, "Дорогой месяц", "active")),
    )
    infos = {
        one: _plan_info(),
        forever: _plan_info(duration_value=None, duration_unit="forever",
                            amount=1500),
        group: _plan_info(eligible=False),
        expensive: _plan_info(amount=10001),
    }

    async def get_info(_factory, *, plan_id, **_kw):
        return infos[plan_id]

    monkeypatch.setattr(ui, "get_owner_plan_info", get_info)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("starspage:0"), _bot())
    finally:
        _language.reset(token)
    card = edit.await_args.kwargs
    cbs = _callbacks(card)
    assert "own:starsnative:" + one.hex + ":0" in cbs
    assert "own:starsnative:" + forever.hex + ":0" not in cbs
    assert "own:starsnative:" + group.hex + ":0" not in cbs
    assert "own:starsnative:" + expensive.hex + ":0" not in cbs
    assert "Навсегда" in card["rich_html"]
    assert "1500" in card["rich_html"]
    assert all(not item.startswith("own:starsnative:") or one.hex in item
               for item in cbs)


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_pausing_active_channel_first_shows_clear_confirm(
    monkeypatch, lang,
):
    plan = uuid4()
    project, edit = _prepare(
        monkeypatch, info=_plan_info(native=True),
        plans=((plan, "1 месяц", "active"),),
    )
    pause = AsyncMock()
    monkeypatch.setattr(ui, "pause_owner_stars_price", pause)
    bot = _bot()
    token = _language.set(lang)
    try:
        query = _query("starsoff:" + plan.hex + ":0", message_id=333)
        await _callback(project)(query, bot)
    finally:
        _language.reset(token)
    edit.assert_awaited_once()
    card = edit.await_args.kwargs
    assert card["message_id"] == 333
    assert ("оплаченные подписки" if lang == "ru"
            else "paid subscriptions") in card["rich_html"].lower()
    assert _callbacks(card) == [
        "own:starsoffok:" + plan.hex + ":0", "own:starspage:0",
    ]
    query.answer.assert_awaited_once()
    pause.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_confirm_pause_revokes_native_link_without_bot_fallback(
    monkeypatch, lang,
):
    plan, link = uuid4(), "https://t.me/+native_saved_subscription"
    project, edit = _prepare(
        monkeypatch, info=_plan_info(native=False),
        plans=((plan, "1 месяц", "active"),),
    )
    pause = AsyncMock(return_value=(True, (-100123456789, link)))
    monkeypatch.setattr(ui, "pause_owner_stars_price", pause)
    bot = _bot()
    bot.revoke_chat_invite_link = AsyncMock()
    token = _language.set(lang)
    try:
        await _callback(project)(
            _query("starsoffok:" + plan.hex + ":0", message_id=444), bot,
        )
    finally:
        _language.reset(token)
    pause.assert_awaited_once()
    assert pause.await_args.kwargs["bot_id"] == bot.id
    bot.revoke_chat_invite_link.assert_awaited_once_with(
        chat_id=-100123456789, invite_link=link,
    )
    edit.assert_awaited_once()
    card = edit.await_args.kwargs
    assert card["message_id"] == 444
    assert ("Ранее оплаченные подписки" if lang == "ru"
            else "existing paid subscriptions") in card["rich_html"]
    assert "own:starsnative:" + plan.hex not in _callbacks(card)
    bot.send_message.assert_not_awaited()


async def test_pause_provider_revoke_failure_warns_owner_in_same_card(
    monkeypatch,
):
    plan, link = uuid4(), "https://t.me/+must_revoke_manually"
    project, edit = _prepare(
        monkeypatch, info=_plan_info(native=False),
        plans=((plan, "1 месяц", "active"),),
    )
    monkeypatch.setattr(
        ui, "pause_owner_stars_price",
        AsyncMock(return_value=(True, (-100123456789, link))),
    )
    bot = _bot()
    bot.revoke_chat_invite_link = AsyncMock(
        side_effect=AiogramError("request rejected"),
    )
    await _callback(project)(
        _query("starsoffok:" + plan.hex + ":0", message_id=555), bot,
    )
    card = edit.await_args.kwargs
    assert "не подтвердил отзыв" in card["rich_html"]
    assert link in card["rich_html"]
    assert card["message_id"] == 555
    bot.send_message.assert_not_awaited()


async def test_bot_stars_pause_requires_no_native_confirmation(monkeypatch):
    plan = uuid4()
    project, edit = _prepare(
        monkeypatch, info=_plan_info(),
        plans=((plan, "1 месяц", "active"),),
    )
    pause = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(ui, "pause_owner_stars_price", pause)
    bot = _bot()
    await _callback(project)(
        _query("starsoff:" + plan.hex + ":0"), bot,
    )
    pause.assert_awaited_once()
    assert "own:starsoffok:" + plan.hex not in _callbacks(
        edit.await_args.kwargs,
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(("lang", "phrase"), [
    ("ru", "Для тарифа «Навсегда»"),
    ("en", "Use the bot balance for lifetime plans"),
])
def test_native_duration_error_localized_without_raw_english(lang, phrase):
    token = _language.set(lang)
    try:
        message = owner_error(
            "Channel Stars require a 1-month plan priced at 1–10000 whole Stars"
        )
        assert phrase in message
        assert ("📅" in message)
        assert len(message) <= 180
    finally:
        _language.reset(token)


async def test_existing_monthly_native_activation_replaces_confirm_and_list(
    monkeypatch,
):
    project, edit = _prepare(monkeypatch, info=_plan_info())
    plan = ui.setup_overview.return_value.plans[0][0]
    target = NativeStarsTarget(
        plan_id=plan, price_id=uuid4(), resource_id=uuid4(),
        chat_id=-100123456789, amount=200, bot_id=987,
        invite_link=None, active=False,
    )
    monkeypatch.setattr(
        ui, "read_native_stars_target", AsyncMock(return_value=target),
    )
    bot = _bot()
    await _callback(project)(
        _query("starsnative:" + plan.hex + ":0", message_id=602), bot,
    )
    card = edit.await_args.kwargs
    assert card["message_id"] == 602
    assert "Создать платную ссылку" in card["rich_html"]
    assert _callbacks(card) == [
        "own:starsnativeok:" + plan.hex + ":0",
        "own:starspage:0",
    ]
    bot.send_message.assert_not_awaited()
