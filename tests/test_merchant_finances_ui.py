"""Platform owner merchant-finance screen: compact RU/EN and private callbacks."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.merchant_finances import (
    MerchantFinances,
    platform_merchant_finances,
)


def _snapshot():
    return MerchantFinances(
        period="30", page=0, pages=2, merchants_total=7, unmapped_payments=1,
        businesses=({
            "merchant_id": str(uuid4()),
            "label": "My & Premium <Channel>",
            "payments": 2,
            "currencies": [
                {"currency": "USD", "amount": "12.345", "count": 1},
                {"currency": "RUB", "amount": "500.00", "count": 1},
            ],
        },),
    )


def _callback(uid=7777, data="plat:merchants:30:0"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid, username="tester", first_name="Test"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=uid)),
        answer=AsyncMock(),
    )


def _handler(router):
    return next(
        entry.callback for entry in router.callback_query.handlers
        if entry.callback.__name__ == "platform_callback"
    )


def test_merchant_financial_text_escapes_labels_and_preserves_currency():
    ru = ui._merchant_text(_snapshot())
    assert "Финансы бизнесов" in ru
    assert "My &amp; Premium &lt;Channel&gt;" in ru
    assert "12.345" in ru and "500" in ru
    assert "операций: 1" in ru
    assert "без установленного исторического бизнеса: 1" in ru
    assert "не выручка ViMi Pay" in ru
    assert "Страница 1 из 2" in ru

    token = _language.set("en")
    try:
        en = ui._merchant_text(_snapshot())
        assert "Merchant finances" in en
        assert "transactions: 1" in en
        assert "not independent bank verification" in en
    finally:
        _language.reset(token)


async def test_private_merchant_screen_uses_server_bound_owner_identity(monkeypatch):
    report = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(ui, "platform_merchant_finances", report)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(router)(_callback(uid=8888), bot)
    report.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    await _handler(router)(_callback(), bot)
    report.assert_awaited_once()
    assert report.await_args.kwargs["actor_telegram_id"] == 7777
    callbacks = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
    ]
    assert "plat:merchants:30:1" in callbacks
    assert "plat:home:30:0" in callbacks

    for invalid in ("plat:merchants:BAD:0", "plat:merchants:30:-1",
                    "plat:merchants:30:100001"):
        callback = _callback(data=invalid)
        await _handler(router)(callback, bot)
        assert callback.answer.await_args.kwargs["show_alert"] is True
    assert report.await_count == 1


async def test_merchant_finances_rejects_unconfigured_owner():
    with pytest.raises(PermissionError):
        await platform_merchant_finances(
            MagicMock(), actor_user_id=uuid4(), actor_telegram_id=7777,
            settings=Settings(platform_owner_telegram_id=None),
        )


def _large_snapshot():
    currencies = [
        {
            "currency": f"CODE{i:02}",
            "amount": "12345678901234567890.123456789012345678",
            "count": 123456,
        }
        for i in range(33)
    ]
    return replace(
        _snapshot(),
        businesses=tuple(
            {
                "merchant_id": str(uuid4()),
                "label": "A & B " + ("Premium channel " * 5),
                "payments": 123456,
                "currencies": currencies,
            }
            for _ in range(6)
        ),
    )


def test_large_merchant_report_split_at_html_safe_lines():
    snapshot = _large_snapshot()
    full_text = ui._merchant_text(snapshot)
    chunks = ui._merchant_text_chunks(snapshot)
    assert len(full_text) > 4096
    assert len(chunks) > 1
    assert all(len(part) <= 3500 for part in chunks)
    assert "\n".join(chunks) == full_text
    for part in chunks:
        assert part.count("<b>") == part.count("</b>")
        assert part.count("<i>") == part.count("</i>")
    assert "не выручка ViMi Pay" in chunks[-1]


async def test_large_merchant_report_sends_navigation_only_on_last_chunk(monkeypatch):
    report = AsyncMock(return_value=_large_snapshot())
    monkeypatch.setattr(ui, "platform_merchant_finances", report)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(router)(_callback(), bot)
    calls = bot.send_message.await_args_list
    assert len(calls) > 1
    assert all(len(call.args[1]) <= 3500 for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] is not None
    controls = [
        button.callback_data
        for row in calls[-1].kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "plat:merchants:30:1" in controls
    assert "plat:home:30:0" in controls


def test_large_multiline_business_name_never_breaks_html_chunks():
    snapshot = _large_snapshot()
    businesses = list(snapshot.businesses)
    businesses[0] = {
        **businesses[0],
        "label": "Elite & Partners\nUltra Premium <Channel>\r\n" + "A" * 65,
    }
    snapshot = replace(snapshot, businesses=tuple(businesses))
    chunks = ui._merchant_text_chunks(snapshot)
    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    assert "Elite &amp; Partners Ultra Premium &lt;Channel&gt;" in "\n".join(chunks)
    assert all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks)
    assert all(chunk.count("<i>") == chunk.count("</i>") for chunk in chunks)
    assert "\n".join(chunks) == ui._merchant_text(snapshot)
