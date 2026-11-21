"""Contract tests for the approved bilingual connection lifecycle cards."""
import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.connection_cards import (
    connection_management_classic,
    connection_management_rich,
    connection_success_classic,
    connection_success_rich,
)
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


@pytest.mark.parametrize(
    ("locale", "success", "next_step", "edit_title", "management", "sync"),
    [
        ("ru", "БОТ УСПЕШНО ПОДКЛЮЧЁН", "ЧТО ДЕЛАТЬ ДАЛЬШЕ",
         "НУЖНО ИЗМЕНИТЬ ПОДКЛЮЧЕНИЕ", "УПРАВЛЕНИЕ ПОДКЛЮЧЕНИЕМ",
         "Синхронизация Managed Bot"),
        ("en", "BOT CONNECTED SUCCESSFULLY", "WHAT TO DO NEXT",
         "NEED TO CHANGE THE CONNECTION", "MANAGE CONNECTION",
         "Managed Bot sync"),
    ],
)
def test_success_and_management_have_rich_and_classic_ru_en(
    locale, success, next_step, edit_title, management, sync,
):
    language = _language.set(locale)
    try:
        rich_success = connection_success_rich(
            username="vimi_testbot", imported="applied",
        )
        classic_success = connection_success_classic(
            username="vimi_testbot", imported="applied",
        )
        args = dict(
            project_name="vimi_testbot",
            bots=(("vimi_testbot", "registered"),),
            runtime_label="⚪ Waiting to start",
            retiring=False,
            has_instances=True,
            editable=True,
            danger_kind="detach",
        )
        rich_manage = connection_management_rich(**args)
        classic_manage = connection_management_classic(**args)
    finally:
        _language.reset(language)

    assert all(term in rich_success for term in (
        success, next_step, edit_title, "@vimi_testbot",
        "1️⃣", "2️⃣", "3️⃣", "<b>", "<i>",
        RICH_ACTIONS_MARKER,
    ))
    assert success in classic_success
    assert "@" + "vimi_testbot" in classic_success
    assert management in rich_manage
    assert sync in rich_manage
    assert "📡" in rich_manage and "🧾" in rich_manage
    assert "⏳" not in rich_manage  # runtime remains truthful, not invented
    assert RICH_ACTIONS_MARKER in rich_manage
    assert management in classic_manage
    assert sync in classic_manage


def test_connection_cards_escape_untrusted_names_and_no_raw_ids():
    success = connection_success_rich(
        username="a<&b", imported=None,
    )
    assert "@a&lt;&amp;b" in success
    assert "@a<&b" not in success
    manage = connection_management_rich(
        project_name="Bad <x> &name",
        bots=(("a<&b", "registered"),),
        runtime_label="⚪ <pending>",
        retiring=False, has_instances=True,
        editable=False, danger_kind=None,
    )
    assert "Bad &lt;x&gt; &amp;name" in manage
    assert "@a&lt;&amp;b" in manage
    assert "&lt;pending&gt;" in manage
    assert "🔑" not in manage
    assert "✏️" not in manage
    assert "🗑️" not in manage
    assert "🧾" in manage


def test_success_network_import_failure_is_visible_but_not_a_blocker():
    card = connection_success_classic(
        username="connected_bot", imported="failed",
    )
    assert "Профиль сети не скопирован" in card
    assert "бот подключён" in card.lower()
    assert "@" + "connected_bot" in card


def test_retiring_connection_does_not_offer_token_rotation():
    card = connection_management_rich(
        project_name="Removing",
        bots=(("removing_bot", "deleting"),),
        runtime_label="⚪ Ожидает запуска",
        retiring=True, has_instances=True,
        editable=True, danger_kind=None,
    )
    assert "отключается" in card
    assert "🔑" not in card
    assert "🤖 Синхронизация" not in card
    assert "🧾" in card
