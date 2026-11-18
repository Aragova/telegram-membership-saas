"""Actual central-bot invoice Pay callbacks, with no payments or DB writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize("language",("ru","en"))
@pytest.mark.parametrize("page",[0,10,100000])
async def test_central_account_pay_selector_routes_to_approved_method(
    monkeypatch,language,page,
):
    actor_id=uuid4()
    invoice=uuid4()
    method=uuid4()
    token=encode_uuid(invoice)
    compact=ui._encode_saas_page(page)
    monkeypatch.setattr(ui,"ensure_telegram_actor",
                        AsyncMock(return_value=actor_id))
    selected=AsyncMock(side_effect=AssertionError(
        "Account payment must work without a connected project",
    ))
    monkeypatch.setattr(ui,"get_selected_project",selected)
    balance=AsyncMock(return_value={"remaining_rub":"150.00"})
    catalog=AsyncMock(return_value=(
        {"id":str(method),"currency":"RUB","name":"СБП"},
    ))
    new_attempt=AsyncMock(return_value={
        "id":str(uuid4()),"amount":"150.00","currency":"RUB",
        "rub_per_unit":"1","instructions":"Bank details test 123456",
    })
    monkeypatch.setattr(ui,"saas_v2_invoice_balance",balance)
    monkeypatch.setattr(ui,"saas_catalog_for_invoice",catalog)
    monkeypatch.setattr(ui,"saas_v3_create_attempt",new_attempt)
    bind_prompt = AsyncMock()
    monkeypatch.setattr(ui, "saas_bind_payment_prompt", bind_prompt)
    router=ui.create_owner_setup_router(MagicMock(),fixed_project_id=None)
    callback=next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__=="owner_callback"
    )
    bot=MagicMock()
    bot.id=432
    bot.send_message=AsyncMock()
    bot.send_message.return_value.message_id=561
    source=SimpleNamespace(
        from_user=SimpleNamespace(
            id=777123,username="owner",first_name="Owner",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private",id=777123),
        ),
        answer=AsyncMock(),
    )

    async def choose(action):
        source.data="own:"+action
        await callback(source,bot)
        return bot.send_message.await_args.kwargs["reply_markup"]

    locale=_language.set(language)
    try:
        first=await choose("spm:"+token+":RUB:"+compact)
        rows=first.inline_keyboard
        assert len(rows)==3
        assert [len(row) for row in rows]==[2,2,1]
        assert [b.text for row in rows[:2] for b in row]==[
            "💳 Lava.top","⭐ Telegram Stars",
            "💎 Crypto Pay",("🏦 Полуавтомат" if language=="ru"
                             else "🏦 Manual"),
        ]
        assert "own:saasinvoices:0" == rows[2][0].callback_data
        # All payment choices preserve the original invoice and page token.
        for code in ("L","S","C","M"):
            assert "own:spr:"+token+":"+code+":"+compact in {
                b.callback_data for row in rows for b in row
            }
        # Existing manual transfer still goes through the original
        # central catalog and the original proof/approval flow.
        await choose("spr:"+token+":M:"+compact)
        manual=await choose("spb:"+token+":RUB:"+compact)
        nxt="own:spv:"+token+":"+encode_uuid(method)+":RUB:"+compact
        assert nxt in {b.callback_data for row in manual.inline_keyboard
                       for b in row}
        await choose(nxt[4:])
        assert new_attempt.await_args.kwargs["invoice_id"]==invoice
        assert new_attempt.await_args.kwargs["method_id"]==method
        assert new_attempt.await_args.kwargs["currency"]=="RUB"
        assert "Bank details test 123456" in bot.send_message.await_args.args[1]
        assert "🔐 Код заявки" not in bot.send_message.await_args.args[1]
        assert "Reference" not in bot.send_message.await_args.args[1]
        bind_prompt.assert_awaited_once()
        assert bind_prompt.await_args.kwargs["source_message_id"] == 561
        assert bind_prompt.await_args.kwargs["source_bot_id"] == 432
        assert "own:saasinvoices:0" in {
            button.callback_data
            for row in bot.send_message.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard for button in row
        }
    finally:
        _language.reset(locale)
    selected.assert_not_awaited()
    assert balance.await_count==4
    assert catalog.await_count==3


@pytest.mark.parametrize("language", ("ru", "en"))
@pytest.mark.parametrize("rail,prefix,heading", [
    ("L", "sl_", "💳 <b>Lava.top · ViMi Pay</b>"),
    ("S", "sa_", "⭐ <b>"),
    ("C", "sc_", "💎 <b>Crypto Pay · ViMi Pay</b>"),
])
async def test_tenant_saas_automatic_rail_handoff_keeps_original_invoice(
    monkeypatch, language, rail, prefix, heading,
):
    """Connected-bot buttons only open payer-bound central handoffs.

    They must not mutate invoice balances or call provider API in a merchant
    bot. Text contains real newlines, not escaped backslash-n artifacts.
    """
    actor_id, project_id, invoice = uuid4(), uuid4(), uuid4()
    token = encode_uuid(invoice)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor",
        AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        ui, "can_manage_project",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        ui, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    step = AsyncMock()
    monkeypatch.setattr(ui, "_show_saas_payment_step", step)
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.id = 9917
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        data="own:spr:" + token + ":" + rail + ":0",
        from_user=SimpleNamespace(
            id=9871, username="payer", first_name="Owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=9871)),
        answer=AsyncMock(),
    )

    locale = _language.set(language)
    try:
        await callback(query, bot)
        step.assert_awaited_once()
        html, rows = step.await_args.args[2:]
        assert heading in html
        assert "\\n" not in html
        assert "\n" in html
        assert rows[0][0].url == (
            "https://t.me/vimipay_bot?start=" + prefix + token
        )
        assert rows[1][0].callback_data == (
            "own:spm:" + token + ":RUB:0"
        )
    finally:
        _language.reset(locale)
    query.answer.assert_awaited_once()
    bot.send_message.assert_not_awaited()
