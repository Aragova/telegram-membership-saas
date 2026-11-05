"""Premium buyer storefront: compact five-wide flag picker and readable copy.

All assertions are UI-only: no checkout, payment, subscription, or RLS mutations.
"""
from decimal import Decimal
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.buyer_preview import (
    preview_catalog_screen,
    preview_offer_screen,
)
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    CURRENCIES_PER_ROW,
    build_buyer_offers,
    catalog_screen,
    offer_screen,
)


def _offers(codes=("RUB", "USD", "EUR", "UAH", "BYN"), *, name="1 месяц"):
    plan_id = uuid4()
    options = tuple(
        CheckoutOption(
            plan_id=plan_id,
            plan_name=name,
            duration_value=1,
            duration_unit="month",
            plan_price_id=uuid4(),
            currency_code=code,
            amount=Decimal("550"),
            currency_precision=2,
            payment_method_id=uuid4(),
            payment_method_name="Банк",
        )
        for code in codes
    )
    return build_buyer_offers(
        options, resources={plan_id: ("RUSSIAN CHANNEL PREMIUM",)},
    )


def test_five_local_currencies_share_exactly_one_row_with_bright_selection():
    offers = _offers()
    text, keyboard = catalog_screen(offers)
    first_row = keyboard.inline_keyboard[0]
    assert CURRENCIES_PER_ROW == 5
    assert len(first_row) == 5
    assert [button.callback_data for button in first_row] == [
        "cat:RUB:0", "cat:USD:0", "cat:EUR:0", "cat:UAH:0", "cat:BYN:0",
    ]
    assert first_row[0].text == "🟢🇷🇺RUB"
    assert first_row[0].style == "success"
    assert [button.text for button in first_row[1:]] == [
        "🇺🇸USD", "🇪🇺EUR", "🇺🇦UAH", "🇧🇾BYN",
    ]
    assert all(button.style is None for button in first_row[1:])
    assert "Добро пожаловать" in text
    assert "🌍 Валюта: <b>🇷🇺 RUB</b>" in text
    assert "1 месяц · 1 месяц" not in text
    assert "550 RUB" in text
    assert "👇" in text

    ua_text, ua_keyboard = catalog_screen(offers, currency="UAH")
    selected = ua_keyboard.inline_keyboard[0]
    assert len(selected) == 5
    assert [b.style for b in selected] == [None, None, None, "success", None]
    assert selected[3].text == "🟢🇺🇦UAH"
    assert selected[0].text == "🇷🇺RUB"
    assert "550 UAH" in ua_text
    assert "550 RUB" not in ua_text


def test_checkout_and_preview_keep_five_currencies_and_payment_action_safe():
    offers = _offers()
    byn = next(o for o in offers if o.option.currency_code == "BYN")
    screen = offer_screen(offers, price_id=byn.option.plan_price_id)
    assert screen is not None
    text, keyboard = screen
    assert len(keyboard.inline_keyboard[0]) == 5
    assert keyboard.inline_keyboard[0][4].text == "🟢🇧🇾BYN"
    assert keyboard.inline_keyboard[0][4].style == "success"
    assert "✨ Отличный выбор!" in text
    assert "💰 <b>550 BYN</b>" in text
    assert "RUSSIAN CHANNEL PREMIUM" in text
    buy = [
        b for row in keyboard.inline_keyboard for b in row
        if b.callback_data.startswith("buy:")
    ]
    assert len(buy) == 1
    assert buy[0].text == "💳 Банк"

    preview_text, preview_keyboard = preview_catalog_screen(offers)
    assert "Предпросмотр" in preview_text
    assert "Добро пожаловать" in preview_text
    first = preview_keyboard.inline_keyboard[0]
    assert len(first) == 5
    assert [b.callback_data for b in first] == [
        "own:pvcat:RUB:0", "own:pvcat:USD:0", "own:pvcat:EUR:0",
        "own:pvcat:UAH:0", "own:pvcat:BYN:0",
    ]
    assert first[0].style == "success"
    rendered = preview_offer_screen(offers, price_id=byn.option.plan_price_id)
    assert rendered is not None
    _, pkeyboard = rendered
    assert len(pkeyboard.inline_keyboard[0]) == 5
    assert pkeyboard.inline_keyboard[0][4].style == "success"
    assert all(
        not button.callback_data.startswith("buy:")
        for row in pkeyboard.inline_keyboard for button in row
    )


def test_extra_currencies_wrap_in_groups_of_five_without_duplicate_buttons():
    offers = _offers(
        ("RUB", "USD", "EUR", "UAH", "BYN", "KZT", "BTC"),
    )
    text, keyboard = catalog_screen(offers)
    assert "550 RUB" in text
    assert [len(row) for row in keyboard.inline_keyboard[:2]] == [5, 2]
    buttons = keyboard.inline_keyboard[0] + keyboard.inline_keyboard[1]
    assert len({b.callback_data for b in buttons}) == 7
    assert {b.text for b in buttons} >= {"🇰🇿KZT", "₿BTC"}
    assert all(len(b.callback_data.encode("utf-8")) <= 64 for b in buttons)


def test_english_copy_and_html_escaping_are_preserved():
    token = _language.set("en")
    try:
        offers = _offers(("RUB", "USD"), name="Premium <Gold> & Friends")
        text, keyboard = catalog_screen(offers)
        assert "👋 Welcome!" in text
        assert "Premium &lt;Gold&gt; &amp; Friends" in text
        assert "Currency:" in text
        selected = next(o for o in offers if o.option.currency_code == "RUB")
        offer_text, _ = offer_screen(
            offers, price_id=selected.option.plan_price_id,
        )
        assert "Great choice!" in offer_text
        assert "Premium &lt;Gold&gt; &amp; Friends" in offer_text
        assert "How would you like to pay?" in offer_text
        assert len(keyboard.inline_keyboard[0]) == 2
    finally:
        _language.reset(token)
