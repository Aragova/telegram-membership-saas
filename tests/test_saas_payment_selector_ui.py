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
    router=ui.create_owner_setup_router(MagicMock(),fixed_project_id=None)
    callback=next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__=="owner_callback"
    )
    bot=MagicMock()
    bot.id=432
    bot.send_message=AsyncMock()
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
        buttons={button.callback_data for row in first.inline_keyboard
                 for button in row}
        assert "own:saasinvoices:0" in buttons
        nxt="own:spv:"+token+":"+encode_uuid(method)+":RUB:"+compact
        assert nxt in buttons
        amount_page=await choose(nxt[4:])
        prices={button.callback_data for row in amount_page.inline_keyboard
                for button in row}
        pay="own:spc:"+token+":"+encode_uuid(method)+":RUB:F:"+compact
        assert pay in prices
        assert len(pay.encode("utf-8"))<=64
        await choose(pay[4:])
        assert new_attempt.await_args.kwargs["invoice_id"]==invoice
        assert new_attempt.await_args.kwargs["method_id"]==method
        assert new_attempt.await_args.kwargs["currency"]=="RUB"
        assert "Bank details test 123456" in bot.send_message.await_args.args[1]
        assert "own:saasinvoices:0" in {
            button.callback_data
            for row in bot.send_message.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard for button in row
        }
    finally:
        _language.reset(locale)
    selected.assert_not_awaited()
    assert balance.await_count==3
    assert catalog.await_count==3
