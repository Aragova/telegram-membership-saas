"""Telegram Stars always has one human label; the Bot API keeps XTR."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from telegram_membership_saas.currency_display import (
    STARS_DISPLAY_NAME,
    display_currency,
    display_price,
)
from telegram_membership_saas.modules.telegram_commerce.rich_ui import (
    rich_subscription_offer_html,
)
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    BuyerOffer,
    catalog_screen,
    offer_screen,
    rich_catalog_html,
)


@pytest.mark.parametrize("code,expected", [
    ("XTR", "⭐ Telegram Stars"),
    ("RUB", "RUB"),
    ("USD", "USD"),
    ("EUR", "EUR"),
])
def test_currency_display_is_presentation_only(code, expected):
    assert display_currency(code) == expected
    assert display_price("500", code) == "500 " + expected


def _stars_offer() -> BuyerOffer:
    option = CheckoutOption(
        plan_id=uuid4(), plan_name="1 месяц",
        duration_value=1, duration_unit="month",
        plan_price_id=uuid4(), currency_code="XTR",
        amount=Decimal("500"), currency_precision=0,
        payment_method_id=UUID(int=0),
        payment_method_name=STARS_DISPLAY_NAME,
    )
    return BuyerOffer(
        option=option, methods=(option,), resource_label="Premium channel",
    )


def test_stars_catalog_offer_and_rich_have_no_visible_xtr():
    offer = _stars_offer()
    text, keys = catalog_screen((offer,), currency="XTR")
    assert "⭐ Telegram Stars" in text
    assert "500 ⭐ Telegram Stars" in text
    assert "XTR" not in text
    labels = [button.text for row in keys.inline_keyboard for button in row]
    assert any("⭐ Telegram Stars" in label for label in labels)
    assert not any("XTR" in label for label in labels)
    # Machine callbacks MUST retain the Telegram currency code.
    callbacks = [button.callback_data for row in keys.inline_keyboard
                 for button in row]
    assert "cat:XTR:0" in callbacks

    detail = offer_screen((offer,), price_id=offer.option.plan_price_id)
    assert detail is not None
    body, buttons = detail
    assert "500 ⭐ Telegram Stars" in body
    assert "XTR" not in body
    assert any(
        "⭐ Telegram Stars" in button.text
        for row in buttons.inline_keyboard for button in row
    )
    rich = rich_catalog_html((offer,), currency="XTR")
    assert rich is not None
    assert "⭐ Telegram Stars" in rich
    assert "500 ⭐ Telegram Stars" in rich
    assert "XTR" not in rich
    offer_rich = rich_subscription_offer_html(offer)
    assert "500 ⭐ Telegram Stars" in offer_rich
    assert "XTR" not in offer_rich
