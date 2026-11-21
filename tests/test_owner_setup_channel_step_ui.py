"""Contract for the approved 1/4 ViMi Pay channel setup card (RU + EN).

The text must use the *real connected bot* and exactly the Telegram
permissions the live verification service requires. No test Telegram I/O.
"""
from html import unescape

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.onboarding_ui import (
    owner_setup_step_classic,
    owner_setup_step_rich,
    wizard_primary_label,
)
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


@pytest.mark.parametrize(
    "locale,title,guide,role,invite,restrict,action",
    [
        (
            "ru", "Настройка ViMi Pay", "КАК ЭТО СДЕЛАТЬ?",
            "ЭТОГО бота",
            "Добавление пользователей",
            "Блокировка / удаление пользователей",
            "📢 Подключить канал / группу",
        ),
        (
            "en", "ViMi Pay setup", "HOW TO CONNECT?",
            "THIS bot",
            "Add users",
            "Ban / remove users",
            "📢 Connect channel / group",
        ),
    ],
)
def test_channel_setup_rich_classic_and_action_match_approved_mockup(
    locale, title, guide, role, invite, restrict, action,
):
    token = _language.set(locale)
    try:
        rich = owner_setup_step_rich(
            bot_name="vimitest21bot", timezone="Europe/Moscow",
            action="resources",
        )
        classic = owner_setup_step_classic(
            bot_name="vimitest21bot", timezone="Europe/Moscow",
            action="resources",
        )
        label = wizard_primary_label("resources", "old button")
    finally:
        _language.reset(token)

    assert label == action
    for html in (rich, classic):
        for expected in (
            title, guide, role, invite, restrict,
            "1️⃣", "2️⃣", "3️⃣", "@vimitest21bot",
            "Europe/Moscow", "1/4", "ViMi Pay",
        ):
            assert expected in html
        assert html.count("@vimitest21bot") >= 2
        assert "/cancel" not in html
        assert "Language / Язык" not in html
        assert "Проверит ваши права и права бота" not in html
        assert ("Сохранённые тарифы" if locale == "ru"
                else "Saved plans") in html
    assert RICH_ACTIONS_MARKER in rich
    assert RICH_ACTIONS_MARKER not in classic
    assert "<h3>" in rich and "<table" in rich
    assert "<h3>" not in classic and "<table" not in classic
    assert "своего бота" not in rich
    assert len(rich) < 2000  # Step 1 stays short on a phone display.
    assert "<b>1️⃣</b>" in rich and "<b>2️⃣</b>" in rich
    assert "<b>3️⃣</b>" in rich
    assert "🛡" in rich


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_channel_setup_escapes_bot_username_and_timezone(locale):
    token = _language.set(locale)
    try:
        rich = owner_setup_step_rich(
            bot_name="@test<&>bot", timezone="<Area & City>",
            action="resources",
        )
        classic = owner_setup_step_classic(
            bot_name="@test<&>bot", timezone="<Area & City>",
            action="resources",
        )
    finally:
        _language.reset(token)
    for html in (rich, classic):
        assert "@test&lt;&amp;&gt;bot" in html
        assert "&lt;Area &amp; City&gt;" in html
        assert "<Area & City>" not in html
        assert "@@test" not in html
        assert "@test<&>bot" not in html
        assert "test<&>bot" == unescape(
            "test&lt;&amp;&gt;bot"
        )


def test_other_wizard_steps_preserve_existing_content_and_contextual_button():
    token = _language.set("en")
    try:
        for action, expected in (
            ("methods", "Buyer payments"),
            ("plans", "First plan"),
            ("saasprofile", "Service currency"),
        ):
            rich = owner_setup_step_rich(
                bot_name="bot", timezone="Europe/Moscow", action=action,
            )
            classic = owner_setup_step_classic(
                bot_name="bot", timezone="Europe/Moscow", action=action,
            )
            assert expected in rich
            assert "ViMi Pay setup" in classic
            assert wizard_primary_label(action, expected) == expected
    finally:
        _language.reset(token)
