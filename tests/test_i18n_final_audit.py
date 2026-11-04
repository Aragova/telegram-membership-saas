"""Final bilingual UI audit: service errors and central legacy redirects."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.error_locale import owner_error
from telegram_membership_saas.modules.owner_setup.obligation_locale import obligation_error
from telegram_membership_saas.modules.telegram_admin.redirects import (
    create_central_cutover_router,
)
from telegram_membership_saas.modules.pricing.service import _normalize_amount


def test_owner_service_errors_are_localized_without_changing_service_text() -> None:
    sample = "Вы должны быть администратором выбранного канала"
    token = _language.set("en")
    try:
        assert owner_error(sample) == (
            "You must be an administrator of the selected channel."
        )
        assert "already connected" in owner_error(
            "Этот Telegram-бот уже подключён в другом проекте"
        )
        assert "4,000" in owner_error(
            "Инструкция слишком длинная (максимум 4000 символов)"
        )
        assert owner_error("Invalid token format. Get it from BotFather.") == (
            "Invalid token format. Get it from BotFather."
        )
        assert owner_error("Plan name must be 2–120 characters") == (
            "Plan name must be 2–120 characters"
        )
        assert owner_error("Payment method name must be 2–120 characters") == (
            "Payment method name must be 2–120 characters"
        )
        assert owner_error("internal-db-identifier-123") == (
            "Action unavailable. Reopen the section."
        )
    finally:
        _language.reset(token)
    assert owner_error(sample) == sample
    assert owner_error("internal-db-identifier-123") == "internal-db-identifier-123"


async def test_central_old_payment_actions_redirect_in_english() -> None:
    router = create_central_cutover_router()
    old_button = router.callback_query.handlers[0].callback
    old_command = router.message.handlers[0].callback
    query = SimpleNamespace(answer=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=500),
        chat=SimpleNamespace(type="private"),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    token = _language.set("en")
    try:
        await old_button(query)
        query.answer.assert_awaited_once()
        assert "connected bot" in query.answer.await_args.args[0]
        assert query.answer.await_args.kwargs["show_alert"] is True
        await old_command(message, bot)
        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.kwargs["text"]
        assert "Purchases and payment reviews have moved" in text
        assert "/payments" in text and "/start" in text
    finally:
        _language.reset(token)
    bot.send_message.reset_mock()
    await old_command(message, bot)
    assert "Управление оплатами" in bot.send_message.await_args.kwargs["text"]


async def test_connected_owner_error_callback_stays_authorized_and_english(
    monkeypatch,
) -> None:
    project_id = uuid4()
    actor_id = uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        owner_ui, "can_manage_project", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        owner_ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    setup = AsyncMock(
        side_effect=ValueError("Вы должны быть администратором выбранного канала"),
    )
    monkeypatch.setattr(owner_ui, "setup_overview", setup)
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    query = SimpleNamespace(
        data="own:resources",
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(
            id=500, username="test_owner", first_name="Owner",
        ),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock()
    token = _language.set("en")
    try:
        await callback(query, bot)
        assert query.answer.await_args.kwargs["show_alert"] is True
        assert query.answer.await_args.args[0] == (
            "You must be an administrator of the selected channel."
        )
        setup.assert_awaited_once()
        bot.send_message.assert_not_awaited()
    finally:
        _language.reset(token)


def test_obligation_errors_preserve_safe_english_only() -> None:
    token = _language.set("en")
    try:
        assert obligation_error("Action is stale. Refresh obligations.") == (
            "Action is stale. Refresh obligations."
        )
        assert obligation_error("Действие доступно только владельцу проекта") == (
            "Only the project owner can perform this action."
        )
        assert obligation_error("raw-driver-error-with-ids") == (
            "Action unavailable. Refresh the list."
        )
    finally:
        _language.reset(token)


@pytest.mark.parametrize(
    ("amount", "max_fractional_digits", "english", "russian"),
    [
        (
            "0", 2, "Price amount must be finite and positive",
            "Цена должна быть положительным конечным числом.",
        ),
        (
            "not-a-number", 2, "Price amount must be a valid decimal number",
            "Введите корректную числовую сумму.",
        ),
        (
            "1.234", 2, "Price amount supports at most 2 fractional digits",
            "Цена: максимум 2 знаков после запятой.",
        ),
        (
            "1e21", 2, "Price amount supports at most 20 integer digits",
            "Цена: максимум 20 цифр до запятой.",
        ),
    ],
)
def test_real_price_validation_errors_are_actionable_in_both_languages(
    amount: str, max_fractional_digits: int, english: str, russian: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        _normalize_amount(
            Decimal(amount) if amount != "not-a-number" else amount,
            max_fractional_digits=max_fractional_digits,
        )
    assert str(captured.value) == english
    token = _language.set("en")
    try:
        assert owner_error(str(captured.value)) == english
    finally:
        _language.reset(token)
    assert owner_error(str(captured.value)) == russian


def test_untrusted_price_error_variants_remain_redacted_in_english() -> None:
    token = _language.set("en")
    try:
        for unknown in (
            "Price amount supports at most 99 fractional digits",
            "Price amount supports at most 21 integer digits",
            "Price amount supports at most 2 fractional digits; db user: 123",
            "Price amount must be finite and positive [internal info]",
        ):
            assert owner_error(unknown) == "Action unavailable. Reopen the section."
    finally:
        _language.reset(token)
