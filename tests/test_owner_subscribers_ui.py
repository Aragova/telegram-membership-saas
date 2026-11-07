"""UI regression: subscriber management stays in the connected owner bot."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.subscribers import (
    ManualAccessChange,
    SubscriberAccess,
    SubscriberDetail,
    SubscriberGrant,
    SubscriberInvoice,
    SubscriberInvoicePage,
    SubscriberItem,
    SubscriberPage,
    SubscriberPayment,
    SubscriberSubscription,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 44444
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=100),
    )
    return bot


def _query(value):
    return SimpleNamespace(
        data="own:" + value,
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=123),
            message_id=222,
        ),
        answer=AsyncMock(),
    )


def _callbacks(monkeypatch, project=None):
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=owner,
    ))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(
        return_value=True,
    ))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        return_value=project,
    ))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    cb = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )
    return cb, owner


def _buttons(bot):
    return [
        b.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data
    ]


def _detail(customer, resource, *, owner=True, paid=False):
    return SubscriberDetail(
        customer_id=customer, telegram_id=12345678,
        username="buyer", first_name="Buyer",
        project_timezone="Europe/Moscow", owner_only_actions=owner,
        subscriptions=(SubscriberSubscription(
            plan_name="Premium", status="active", period_end=None,
        ),) if paid else (),
        payments=(SubscriberPayment(
            id=uuid4(), plan_name="Premium", amount="550.000000000000000000",
            currency="RUB", approved_at=datetime.now(UTC),
        ),) if paid else (),
        total_payments=1 if paid else 0, history_page=0,
        grants=(SubscriberGrant(
            id=uuid4(), resource_id=resource,
            resource_name="Channel", source="manual", valid_until=None,
        ),),
        total_grants=1, grant_page=0,
        access=(SubscriberAccess(
            resource_name="Channel", desired=True, actual="unknown",
        ),),
        resources=((resource, "Channel"),),
    )


async def test_directory_filters_search_and_page_keep_callbacks_short(monkeypatch):
    project, customer = uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    listing = AsyncMock(return_value=SubscriberPage(
        total=21, page=0, status="all", search="",
        customers=(SubscriberItem(
            customer, 12345678, "buyer", "First", True, False, True,
        ),),
    ))
    monkeypatch.setattr(ui, "list_subscribers", listing)
    bot = _bot()
    await cb(_query("subscribers"), bot)
    actions = _buttons(bot)
    assert "own:subscriber:" + encode_uuid(customer) in actions
    assert "own:sublist:active:0" in actions
    assert "own:sublist:expiring:0" in actions
    assert "own:sublist:expired:0" in actions
    assert "own:sublist:all:1" in actions
    assert "own:subsearch" in actions
    assert all(len(x.encode()) <= 64 for x in actions)

    await cb(_query("sublist:expiring:1"), bot)
    assert listing.await_args.kwargs["status"] == "expiring"
    assert listing.await_args.kwargs["page"] == 1

    await cb(_query("subfind:0:buyer"), bot)
    assert listing.await_args.kwargs["search"] == "buyer"


async def test_subscriber_card_has_history_and_owner_only_manual_actions(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    detail = AsyncMock(return_value=_detail(
        customer, resource, owner=True, paid=True,
    ))
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    bot = _bot()
    await cb(_query("subscriber:" + encode_uuid(customer)), bot)
    text = bot.send_message.await_args.args[1]
    actions = _buttons(bot)
    assert "550 RUB" in text
    assert "550.000000000000000000" not in text
    assert "Premium" in text
    assert "Channel" in text
    cid = encode_uuid(customer)
    assert "own:subactivate:" + cid in actions
    assert "own:subcancel:" + cid in actions
    assert "own:submsg:" + cid in actions
    assert "own:subbanask:" + cid + ":1" in actions

    detail.return_value = _detail(customer, resource, owner=False)
    await cb(_query("subscriber:" + encode_uuid(customer)), bot)
    actions = _buttons(bot)
    assert not any(x.startswith((
        "own:subactivate:", "own:subcancel:", "own:submsg:", "own:subbanask:",
    )) for x in actions)
    assert "own:subscribers" in actions


async def test_manual_access_has_explicit_confirmation_before_any_write(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=_detail(customer, resource),
    ))
    grant = AsyncMock(return_value=ManualAccessChange(
        changed=True, grant_id=uuid4(), telegram_id=12345678,
    ))
    monkeypatch.setattr(ui, "grant_manual_access", grant)
    bot = _bot()
    cid, rid = encode_uuid(customer), encode_uuid(resource)
    await cb(_query("subgrantstart:" + cid), bot)
    assert "own:subtarget:" + cid + ":" + rid in _buttons(bot)
    await cb(_query("subtarget:" + cid + ":" + rid), bot)
    assert all(
        "own:subgrantask:" + cid + ":" + rid + ":" + str(days)
        in _buttons(bot) for days in (7, 30, 90, 0)
    )
    await cb(_query("subgrantask:" + cid + ":" + rid + ":30"), bot)
    assert grant.await_count == 0
    assert "own:subgrantok:" + cid + ":" + rid + ":30" in _buttons(bot)
    await cb(_query("subgrantok:" + cid + ":" + rid + ":30"), bot)
    grant.assert_awaited_once()
    assert grant.await_args.kwargs["project_id"] == project
    assert grant.await_args.kwargs["customer_id"] == customer
    assert grant.await_args.kwargs["resource_id"] == resource
    assert grant.await_args.kwargs["days"] == 30


async def test_revoke_manual_only_and_reject_fake_paid_grant(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    record = _detail(customer, resource)
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=record,
    ))
    revoke = AsyncMock(return_value=ManualAccessChange(
        changed=True, grant_id=record.grants[0].id,
        telegram_id=12345678,
    ))
    monkeypatch.setattr(ui, "revoke_manual_access", revoke)
    bot = _bot()
    cid, gid = encode_uuid(customer), encode_uuid(record.grants[0].id)
    await cb(_query("subrevoke:" + cid + ":" + gid + ":0"), bot)
    assert revoke.await_count == 0
    assert "own:subrevok:" + cid + ":" + gid + ":0" in _buttons(bot)
    await cb(_query("subrevok:" + cid + ":" + gid + ":0"), bot)
    revoke.assert_awaited_once()

    revoke.reset_mock()
    req = _query("subrevoke:" + cid + ":" + encode_uuid(uuid4()) + ":0")
    await cb(req, bot)
    revoke.assert_not_awaited()
    assert req.answer.await_args.kwargs.get("show_alert")


async def test_central_bot_rejects_subscriber_admin_callbacks(monkeypatch):
    cb, _ = _callbacks(monkeypatch, None)
    bot = _bot()
    req = _query("subscribers")
    await cb(req, bot)
    req.answer.assert_awaited()
    assert req.answer.await_args.kwargs.get("show_alert")
    bot.send_message.assert_not_awaited()



async def test_older_manual_grant_can_be_revoked_from_paginated_list(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    target = uuid4()
    current = _detail(customer, resource)
    from dataclasses import replace

    # An older manual entitlement is on page 4 after 32 newer grants.
    older = SubscriberGrant(
        id=target, resource_id=resource, resource_name="Old Channel",
        source="manual", valid_until=None,
    )
    detail = AsyncMock(return_value=replace(
        current, grants=(older,), total_grants=33, grant_page=4,
    ))
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    bot = _bot()
    cid, gid = encode_uuid(customer), encode_uuid(target)
    await cb(_query("subgrantpage:" + cid + ":4"), bot)
    actions = _buttons(bot)
    assert "own:subcancel:" + cid in actions
    assert "own:subgrantpage:" + cid + ":3" in actions
    assert all(len(a.encode("utf-8")) <= 64 for a in actions)
    await cb(_query("subrevoke:" + cid + ":" + gid + ":4"), bot)
    assert "own:subrevok:" + cid + ":" + gid + ":4" in _buttons(bot)
    assert detail.await_args.kwargs["grant_page"] == 4

async def test_subscriber_statuses_ru_en_and_buttons_are_translated(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    monkeypatch.setattr(
        ui, "subscriber_detail",
        AsyncMock(return_value=_detail(customer, resource, paid=True)),
    )
    for language, subscription, actual in (
        ("ru", "активна", "не подтверждён"),
        ("en", "active", "not verified"),
    ):
        token = _language.set(language)
        try:
            bot = _bot()
            await cb(_query("subscriber:" + encode_uuid(customer)), bot)
            body = bot.send_message.await_args.args[1]
            assert "Premium" in body
            assert subscription in body
            assert actual in body
            labels = [
                b.text for row in
                bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
                for b in row
            ]
            assert any(
                ("Activate" if language == "en" else "Активировать")
                in label for label in labels
            )
            if language == "en":
                assert "Подписки" not in body
                assert "Основания доступа" not in body
        finally:
            _language.reset(token)


async def test_invoice_history_from_customer_card_is_compact_and_paginated(monkeypatch):
    from dataclasses import replace

    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    detail = replace(
        _detail(customer, resource), total_invoices=9, pending_invoices=2,
    )
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(return_value=detail))
    invoice = uuid4()
    page = AsyncMock(return_value=SubscriberInvoicePage(
        customer_id=customer, telegram_id=12345678,
        username="buyer", first_name="Buyer", project_timezone="Europe/Moscow",
        total=9, page=0, invoices=(
            SubscriberInvoice(
                id=invoice, status="waiting_review", amount="7.000000000000000000",
                currency="EUR", plan_name="Monthly 💎",
                created_at=datetime.now(UTC),
            ),
        ),
    ))
    monkeypatch.setattr(ui, "list_subscriber_invoices", page)
    bot = _bot()
    cid = encode_uuid(customer)

    await cb(_query("subscriber:" + cid), bot)
    assert "Счетов: 9" in bot.send_message.await_args.args[1]
    assert "own:subinvoices:" + cid + ":0" in _buttons(bot)

    await cb(_query("subinvoices:" + cid + ":0"), bot)
    actions = _buttons(bot)
    assert "7 EUR" in bot.send_message.await_args.args[1]
    assert "7.000000000000000000" not in bot.send_message.await_args.args[1]
    assert "Monthly 💎" in bot.send_message.await_args.args[1]
    assert "own:invoice:" + encode_uuid(invoice) in actions
    assert any("7 EUR" in b.text for row in bot.send_message.await_args.kwargs[
        "reply_markup"].inline_keyboard for b in row)
    assert "own:subinvoices:" + cid + ":1" in actions
    assert "own:subscriber:" + cid in actions
    assert all(len(key.encode()) <= 64 for key in actions)
    assert page.await_args.kwargs["project_id"] == project
    assert page.await_args.kwargs["customer_id"] == customer

    await cb(_query("subinvoices:" + cid + ":1"), bot)
    assert page.await_args.kwargs["page"] == 1


async def test_invoice_history_is_denied_in_central_bot(monkeypatch):
    customer = uuid4()
    cb, _ = _callbacks(monkeypatch, None)
    bot = _bot()
    req = _query("subinvoices:" + encode_uuid(customer) + ":0")
    await cb(req, bot)
    req.answer.assert_awaited()
    assert req.answer.await_args.kwargs.get("show_alert")
    bot.send_message.assert_not_awaited()

