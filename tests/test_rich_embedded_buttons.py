"""Native Bot API 10.3 controls live inside Rich cards with safe fallback."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
    DashboardFinances,
)
from telegram_membership_saas.modules.owner_setup.dashboard_ui import (
    rich_dashboard,
)
from telegram_membership_saas.modules.owner_setup.onboarding_ui import (
    owner_setup_step_classic,
    owner_setup_step_rich,
)
from telegram_membership_saas.rich_messages import (
    RICH_ACTIONS_MARKER,
    rich_row_marker,
    rich_with_inline_actions,
    strip_rich_action_markers,
)


def test_native_buttons_preserve_callbacks_colors_urls_and_escape():
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text='✅ Save & <go>', callback_data="own:save:42",
                style="success",
            ),
            InlineKeyboardButton(
                text="🌐 Open", url="https://t.me/vimi_test2bot",
                style="primary",
            ),
        ],
        [InlineKeyboardButton(
            text="🗑 Delete", callback_data="own:remove:42",
            style="danger",
        )],
    ])
    html = rich_with_inline_actions(
        "<h3>Safe</h3>" + RICH_ACTIONS_MARKER, markup,
    )
    assert html is not None
    assert html.count("<tg-button-row>") == 2
    assert 'data="own:save:42"' in html
    assert 'url="https://t.me/vimi_test2bot"' in html
    assert 'data="own:remove:42"' in html
    assert 'style="success"' in html
    assert 'style="primary"' in html
    assert 'style="danger"' in html
    assert "Save &amp; &lt;go&gt;" in html
    assert "reply_markup" not in html


def test_contextual_pay_button_renders_near_amount_and_back_below_currency():
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Pay", callback_data="own:pay", style="success",
        )],
        [
            InlineKeyboardButton(
                text="💱 Currency", callback_data="own:currency", style="primary",
            ),
            InlineKeyboardButton(
                text="🏠 Home", callback_data="own:home", style="success",
            ),
        ],
    ])
    content = (
        "<h4>Amount due: 500 RUB</h4>" + rich_row_marker(0)
        + "<h4>Future currency: USD</h4>" + rich_row_marker(1)
    )
    html = rich_with_inline_actions(content, markup)
    assert html is not None
    assert html.index("Amount due: 500 RUB") < html.index('data="own:pay"')
    assert html.index('data="own:pay"') < html.index("Future currency: USD")
    assert html.index("Future currency: USD") < html.index('data="own:currency"')
    assert "__VIMI" not in html


def test_unsupported_button_stays_on_classic_keyboard():
    bad = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Pay", pay=True),
    ]])
    assert rich_with_inline_actions("<h3>Pay</h3>", bad) is None
    assert strip_rich_action_markers(
        "<p>safe</p>" + rich_row_marker(0) + RICH_ACTIONS_MARKER
    ) == "<p>safe</p>"


def test_admin_dashboard_has_approved_four_rows_service_stars_and_action_slot():
    language = _language.set("ru")
    try:
        html = rich_dashboard(
            project_name="@vimi_test2bot",
            timezone="Europe/Moscow",
            trial_banner="✅ Обслуживание оплачено до 16.12.2026",
            readiness=(
                ("Канал", True), ("Тариф", True),
                ("Оплата", True), ("Бот", True),
            ),
            sales_status="✅ Продажи доступны.",
            finances=DashboardFinances(bot_stars=None, channels=()),
            channels_count=1,
            active_plans_count=1,
            bot_status="🟢 Работает",
            payment_ready=True,
            service_currency="USD",
            service_debt="0 ₽",
        )
    finally:
        _language.reset(language)
    for label in (
        "Состояние", "Каналы", "Активные тарифы",
        "Оплата покупателей", "🧾 Обслуживание ViMi Pay",
        "⭐ Финансы Telegram", "Недоступен",
    ):
        assert label in html
    assert "1 подключён" in html
    assert "✅ Настроена" in html
    assert "ON</" not in html and "OFF</" not in html
    assert html.index("Состояние") < html.index("Обслуживание ViMi Pay")
    assert html.index("Обслуживание ViMi Pay") < html.index("Финансы Telegram")
    assert html.index("Финансы Telegram") < html.index(RICH_ACTIONS_MARKER)


def test_first_time_wizard_is_bilingual_and_uses_only_existing_actions():
    for language, label in [
        ("ru", "ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ"),
        ("en", "ONE FINAL STEP"),
    ]:
        token = _language.set(language)
        try:
            card = owner_setup_step_rich(
                bot_name="@vimi_test2bot", timezone="Europe/Moscow",
                action="saasprofile",
            )
            fallback = owner_setup_step_classic(
                bot_name="@vimi_test2bot", timezone="Europe/Moscow",
                action="saasprofile",
            )
        finally:
            _language.reset(token)
        assert ("Завершение" if language == "ru" else "Finish") in card
        assert ("Завершение" if language == "ru" else "Finish") in fallback
        assert label in card
        assert RICH_ACTIONS_MARKER in card
        assert "150" in card and "5" in card



def test_rich_dashboard_preserves_trusted_trial_time_and_line_breaks():
    token = _language.set("ru")
    try:
        rich = rich_dashboard(
            project_name="@vimi_test2bot", timezone="Europe/Moscow",
            trial_banner=(
                '🎁 Пробный период: 2 дн.\n🗓️ До: '
                '<tg-time unix="1795200000" format="d">20.11.2026</tg-time>'
            ),
            readiness=(("Канал", False), ("Тариф", False),
                       ("Оплата", False), ("Бот", True)),
            sales_status="⚠️ Требуется настройка",
            finances=DashboardFinances(bot_stars=None, channels=()),
        )
    finally:
        _language.reset(token)
    assert '<tg-time unix="1795200000" format="d">' in rich
    assert "<br>" in rich
    assert "&lt;tg-time" not in rich
