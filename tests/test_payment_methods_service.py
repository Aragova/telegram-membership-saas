import pytest

from telegram_membership_saas.modules.payments.service import render_manual_instruction


def test_manual_instruction_renders_supported_fields() -> None:
    rendered = render_manual_instruction(
        "Переведите {amount} {currency}. Тариф: {plan}. Период: {period}.",
        amount="666",
        currency="RUB",
        plan="1 месяц",
        period="1 месяц",
    )
    assert rendered == "Переведите 666 RUB. Тариф: 1 месяц. Период: 1 месяц."


def test_manual_instruction_allows_literal_braces() -> None:
    rendered = render_manual_instruction(
        "Комментарий: {{VIP}} — {amount} {currency}",
        amount="10",
        currency="USDT",
        plan="Monthly",
        period="1 month",
    )
    assert rendered == "Комментарий: {VIP} — 10 USDT"


@pytest.mark.parametrize(
    "template",
    [
        "Оплатить {user}",
        "Оплатить {amount:.2f}",
        "Оплатить {amount!r}",
        "Оплатить {amount",
    ],
)
def test_manual_instruction_rejects_unsafe_or_unknown_formatting(template: str) -> None:
    with pytest.raises(ValueError):
        render_manual_instruction(
            template,
            amount="10",
            currency="USD",
            plan="Monthly",
            period="1 month",
        )
