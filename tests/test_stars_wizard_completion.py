"""Stars setup must continue to the NEXT missing step, without duplicate plans.

Both bot and native channel Stars use a real active XTR plan. The onboarding
button is scoped to initial setup and never bypasses payment/plan readiness.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_owner_payment_setup_navigation import (
    _bot,
    _callback,
    _callbacks,
    _query,
    _screen,
)

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.onboarding_ui import (
    owner_setup_step_classic,
    owner_setup_step_rich,
)
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.owner_setup.tenant_admin import PriceInfo


def _configured_stars(monkeypatch, project, *, native=False):
    _screen(monkeypatch, project)
    plan = uuid4()
    overview = SetupOverview(
        project_id=project,
        project_name="ViMi Test", timezone="Europe/Moscow",
        bots=(("vimitest21bot", "registered"),),
        bot_instances=((uuid4(), "vimitest21bot", "registered"),),
        resources=((uuid4(), "Private channel", "active"),),
        methods=(),
        plans=((plan, "1 месяц", "active"),),
        sales_configured=True,
    )
    ui.setup_overview.return_value = overview
    monkeypatch.setattr(
        ui, "get_owner_plan_info",
        AsyncMock(return_value=SimpleNamespace(prices=(
            PriceInfo(
                currency="XTR", amount=Decimal("200"), precision=0,
                method_count=0, native_stars_active=native,
            ),
        ))),
    )
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(return_value=()))
    render = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", render)
    monkeypatch.setattr(ui, "create_one_month_stars_plan", AsyncMock())
    return render


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_published_stars_has_forward_for_bot_and_channel(
    monkeypatch, native, lang,
):
    project = uuid4()
    render = _configured_stars(monkeypatch, project, native=native)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("starspage:0", message_id=111), _bot())
    finally:
        _language.reset(token)
    card = render.await_args.kwargs
    assert card["message_id"] == 111
    assert "200 ⭐" in card["rich_html"]
    expected_recipient = (
        ("Канал" if native else "Бот") if lang == "ru"
        else ("Channel" if native else "Bot")
    )
    assert expected_recipient in card["rich_html"]
    assert ("Тариф создан" if lang == "ru" else "Plan created") in card["rich_html"]
    callbacks = _callbacks(card)
    assert "own:setupnext" in callbacks
    assert "own:methods" in callbacks
    assert callbacks.index("own:setupnext") < callbacks.index("own:methods")
    forward = next(
        button for row in card["reply_markup"].inline_keyboard
        for button in row if button.callback_data == "own:setupnext"
    )
    assert forward.style == "success"
    assert forward.text == (
        "➡️ Продолжить настройку" if lang == "ru"
        else "➡️ Continue setup"
    )
    ui.create_one_month_stars_plan.assert_not_awaited()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_setupnext_skips_already_created_stars_plan_to_compact_finish(
    monkeypatch, native, lang,
):
    project = uuid4()
    render = _configured_stars(monkeypatch, project, native=native)
    token = _language.set(lang)
    try:
        await _callback(project)(_query("setupnext", message_id=222), _bot())
    finally:
        _language.reset(token)
    card = render.await_args.kwargs
    assert card["message_id"] == 222
    assert _callbacks(card) == ["own:saasprofile"]
    html = card["rich_html"]
    assert ("Завершение настройки" if lang == "ru"
            else "Finish setting up") in html
    assert ("ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ" if lang == "ru"
            else "ONE FINAL STEP") in html
    assert "150 ₽" in html
    assert "$5" in html
    assert "5%" in html
    assert "vimitest21bot" not in html
    assert "Europe/Moscow" not in html
    assert "vimitest21bot" not in card["classic_html"]
    assert "Europe/Moscow" not in card["classic_html"]
    ui.create_one_month_stars_plan.assert_not_awaited()


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_methods_recognize_stars_without_a_manual_method(monkeypatch, lang):
    project = uuid4()
    render = _configured_stars(monkeypatch, project, native=True)
    token = _language.set(lang)
    try:
        # The Back button on Stars is methods, not methods:guided.
        await _callback(project)(_query("methods", message_id=333), _bot())
    finally:
        _language.reset(token)
    card = render.await_args.kwargs
    assert card["message_id"] == 333
    html = card["rich_html"]
    assert ("Telegram Stars настроены" if lang == "ru"
            else "Telegram Stars configured") in html
    assert ("ДОПОЛНИТЕЛЬНЫЕ СПОСОБЫ" if lang == "ru"
            else "OPTIONAL PAYMENT METHODS") in html
    assert ("ЧТО НУЖНО СДЕЛАТЬ" if lang == "ru"
            else "WHAT TO DO?") not in html
    assert _callbacks(card) == [
        "own:starspage:0", "own:cryptopay", "own:newmethod",
        "own:setupnext", "own:setupback2",
    ]
    ui.create_one_month_stars_plan.assert_not_awaited()


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_finish_card_is_short_rich_and_classic_in_both_languages(lang):
    token = _language.set(lang)
    try:
        for body in (
            owner_setup_step_rich(
                bot_name="should-not-be-displayed", timezone="Europe/Moscow",
                action="saasprofile",
            ),
            owner_setup_step_classic(
                bot_name="should-not-be-displayed", timezone="Europe/Moscow",
                action="saasprofile",
            ),
        ):
            assert "should-not-be-displayed" not in body
            assert "Europe/Moscow" not in body
            assert "150 ₽" in body
            assert "$5" in body
            assert "5%" in body
            assert ("ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ" if lang == "ru"
                    else "ONE FINAL STEP") in body
    finally:
        _language.reset(token)


async def test_setupnext_with_manual_payment_but_no_plan_goes_to_plans(
    monkeypatch,
):
    project = uuid4()
    _screen(monkeypatch, project)
    ui.setup_overview.return_value = SetupOverview(
        project_id=project, project_name="ViMi",
        timezone="Europe/Moscow",
        bots=(("vimitest21bot", "registered"),),
        bot_instances=((uuid4(), "vimitest21bot", "registered"),),
        resources=((uuid4(), "Channel", "active"),),
        plans=(), methods=((uuid4(), "Bank", "active"),),
        sales_configured=False,
    )
    retire = AsyncMock()
    monkeypatch.setattr(ui, "retire_crypto_owner_card", retire)
    bot = _bot()
    await _callback(project)(_query("setupnext", message_id=555), bot)
    assert "Тарифы" in bot.send_message.await_args.args[1]
    assert any(
        button.callback_data == "own:newplan"
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for button in row
    )
    retire.assert_awaited_once_with(bot, 345, 555)


async def test_setupnext_without_payment_cannot_skip_to_final_step(monkeypatch):
    project = uuid4()
    _screen(monkeypatch, project)
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(return_value=[]))
    render = AsyncMock()
    monkeypatch.setattr(ui, "replace_owner_rich", render)
    await _callback(project)(_query("setupnext", message_id=444), _bot())
    card = render.await_args.kwargs
    assert card["message_id"] == 444
    assert "ЧТО НУЖНО СДЕЛАТЬ" in card["rich_html"]
    assert "own:saasprofile" not in _callbacks(card)
    assert "own:setupnext" not in _callbacks(card)
