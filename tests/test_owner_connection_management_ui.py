"""Central registrar gives every project a safe, compact management card."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.connection_management import (
    ConnectionDisposition,
)
from telegram_membership_saas.modules.owner_setup.service import (
    OwnerHome,
    SetupFlow,
    SetupOverview,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _network_summary():
    return {
        "network": {"connected": 1, "healthy": 0, "attention": 1, "down": 0},
        "revenue": [],
        "billing": {"unpaid": 0, "overdue": 0, "overdue_rub": "0", "next": None},
    }


def bot():
    result = MagicMock()
    result.id = 1001001
    result.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=123),
    )
    return result


def query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            message_id=100,
        ),
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        answer=AsyncMock(),
    )


def handler(monkeypatch, *, customer_project=None, selected=None):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        return_value=selected,
    ))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=customer_project,
    )
    return next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )


def overview(project_id, *, has_bot=False, owner=True):
    return SetupOverview(
        project_id=project_id,
        project_name="ViMi Pay E2E",
        timezone="Europe/Moscow",
        bots=(("test_bot", "registered"),) if has_bot else (),
        resources=(),
        plans=(),
        methods=(),
        bot_instances=((uuid4(), "test_bot", "registered"),) if has_bot else (),
        can_delete_bot=owner,
    )


def buttons(reply):
    return [
        x.callback_data for row in reply.kwargs["reply_markup"].inline_keyboard
        for x in row if x.callback_data
    ]


async def test_connection_list_paginates_100_without_overflow(monkeypatch):
    ids = [(uuid4(), f"Brand {i}") for i in range(100)]
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome(tuple(ids), None),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections",
                        AsyncMock(return_value=()))
    cb = handler(monkeypatch)
    instance = bot()
    await cb(query("projects"), instance)
    first = buttons(instance.send_message.await_args)
    assert len([x for x in first if x.startswith("own:select:")]) == 10
    assert "own:projects:1" in first
    assert first.count("own:saasinvoices:0") == 1
    await cb(query("projects:9"), instance)
    last = buttons(instance.send_message.await_args)
    assert len([x for x in last if x.startswith("own:select:")]) == 10
    assert "own:projects:8" in last
    assert "own:projects:10" not in last
    assert all(len(x.encode()) <= 64 for x in last)


async def test_select_unused_connection_shows_rename_and_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "choose_project", AsyncMock())
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    instance = bot()
    await cb(query("select:" + project_id.hex), instance)
    actions = buttons(instance.send_message.await_args)
    assert "own:connrename:" + project_id.hex in actions
    assert actions.count("own:saasinvoices:0") == 1
    assert "own:connremove:" + project_id.hex in actions
    assert "own:projects" in actions
    assert "own:drop:" not in str(actions)
    ui.choose_project.assert_awaited_once()


async def test_connection_with_history_shows_archive_not_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, True),
    ))
    instance = bot()
    await cb(query("bots"), instance)
    actions = buttons(instance.send_message.await_args)
    assert "own:connarchive:" + project_id.hex in actions
    assert "own:connremove:" + project_id.hex not in actions


async def test_connected_bot_has_safe_retirement_not_project_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id, has_bot=True),
    ))
    instance = bot()
    await cb(query("bots"), instance)
    actions = buttons(instance.send_message.await_args)
    assert any(x.startswith("own:drop:") for x in actions)
    assert not any(x.startswith("own:connremove:") for x in actions)


async def test_confirm_deletion_checks_selected_project_and_owner(monkeypatch):
    project_id, other_id = uuid4(), uuid4()
    cb = handler(monkeypatch, selected=other_id)
    monkeypatch.setattr(ui, "remove_unused_connection", AsyncMock())
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    instance = bot()
    req = query("connremoveok:" + project_id.hex)
    await cb(req, instance)
    ui.remove_unused_connection.assert_not_awaited()
    req.answer.assert_awaited_once()
    assert req.answer.await_args.kwargs.get("show_alert")

    # Even when the selected project matches, a nonowner cannot delete.
    ui.get_selected_project.return_value = project_id
    ui.setup_overview.return_value = overview(project_id, owner=False)
    req = query("connremoveok:" + project_id.hex)
    await cb(req, instance)
    ui.remove_unused_connection.assert_not_awaited()
    assert req.answer.await_args.kwargs.get("show_alert")


async def test_owner_deletion_is_two_explicit_steps(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    monkeypatch.setattr(ui, "remove_unused_connection", AsyncMock())
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome((), None),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections",
                        AsyncMock(return_value=()))
    instance = bot()
    await cb(query("connremove:" + project_id.hex), instance)
    assert "own:connremoveok:" + project_id.hex in buttons(
        instance.send_message.await_args,
    )
    # Before a destructive deletion, debt must be explained without adding
    # any new payment menu or pretending an unpaid bill will vanish.
    notice = instance.send_message.await_args.args[1]
    assert "Неоплаченные счета ViMi Pay сохраняются" in notice
    assert "/billing" in notice
    ui.remove_unused_connection.assert_not_awaited()
    await cb(query("connremoveok:" + project_id.hex), instance)
    ui.remove_unused_connection.assert_awaited_once()
    assert "own:projects" not in buttons(instance.send_message.await_args)
    assert "own:new" in buttons(instance.send_message.await_args)


async def test_rename_connection_central_only(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="generation"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    instance = bot()
    await cb(query("connrename:" + project_id.hex), instance)
    assert "название подключения" in instance.send_message.await_args.args[1]
    markup = instance.send_message.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        "own:promptback:generation", "own:promptcancel:generation",
    ]
    ui.set_flow.assert_awaited_once()

    # A saved prompt from another project must never rename the selected one.
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        "connection_rename", uuid4(), {}, 123, "generation",
    )))
    monkeypatch.setattr(ui, "rename_owner_connection", AsyncMock())
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=123, first_name="Owner", username="owner"),
        chat=SimpleNamespace(id=123, type="private"),
        text="New label",
        message_id=200,
        reply_to_message=SimpleNamespace(
            message_id=123,
            from_user=SimpleNamespace(id=instance.id),
        ),
    )
    router = ui.create_owner_setup_router(MagicMock())
    reply_handler = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "owner_reply_handler"
    )
    await reply_handler(reply, instance)
    ui.rename_owner_connection.assert_not_awaited()


async def test_last_archived_connection_is_reachable_from_central_start(monkeypatch):
    archived_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome((), None, archived_count=1),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections", AsyncMock(
        return_value=((archived_id, "Archived E2E"),),
    ))
    monkeypatch.setattr(ui, "set_chat_commands", AsyncMock())
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    monkeypatch.setattr(ui, "read_owner_network_home", AsyncMock(
        return_value=_network_summary(),
    ))
    router = ui.create_owner_setup_router(MagicMock())
    home_message = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "home_message"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        chat=SimpleNamespace(id=123, type="private"),
        text="/menu",
    )
    instance = bot()
    await home_message(message, instance)
    # Main dashboard never auto-opens archive or hides it; entry via /bots.
    assert "own:projects" in buttons(instance.send_message.await_args)
    assert "own:substart" in buttons(instance.send_message.await_args)
    assert buttons(instance.send_message.await_args).count(
        "own:saasinvoices:0",
    ) == 1


async def test_central_first_run_hides_invoices_but_keeps_billing_command(monkeypatch):
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome((), None)),
    )
    monkeypatch.setattr(
        ui, "has_explicit_language_choice", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(ui, "send_owner_rich", AsyncMock())
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "set_chat_commands", AsyncMock())
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    router = ui.create_owner_setup_router(MagicMock())
    home_message = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "home_message"
    )
    bot_instance = bot()
    await home_message(
        SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, first_name="Owner", username="owner",
            ),
            chat=SimpleNamespace(id=123, type="private"),
            text="/menu",
        ),
        bot_instance,
    )
    markup = ui.send_owner_rich.await_args.kwargs["reply_markup"]
    actions = [
        b.callback_data for row in markup.inline_keyboard
        for b in row if b.callback_data
    ]
    assert actions == ["lng:set:ru", "lng:set:en"]
    # Invoice access is still available through the permanent /billing command,
    # including when an older tenant has removed all of its bots.
    assert any(
        item.callback.__name__ == "owner_platform_billing_command"
        for item in router.message.handlers
    )


async def test_central_selected_connection_dashboard_has_invoice_entry(
    monkeypatch,
):
    project_id = uuid4()
    monkeypatch.setattr(
        ui, "owner_home",
        AsyncMock(return_value=OwnerHome(((project_id, "ViMi E2E"),), project_id)),
    )
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(side_effect=AssertionError("Do not reopen selected bot")),
    )
    monkeypatch.setattr(
        ui, "read_owner_network_home", AsyncMock(
            return_value=_network_summary(),
        ),
    )
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "set_chat_commands", AsyncMock())
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    router = ui.create_owner_setup_router(MagicMock())
    home_message = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "home_message"
    )
    instance = bot()
    await home_message(
        SimpleNamespace(
            from_user=SimpleNamespace(
                id=123, first_name="Owner", username="owner",
            ),
            chat=SimpleNamespace(id=123, type="private"),
            text="/menu",
        ),
        instance,
    )
    assert buttons(instance.send_message.await_args).count(
        "own:saasinvoices:0",
    ) == 1


async def test_archived_registry_keeps_direct_invoice_navigation(monkeypatch):
    monkeypatch.setattr(
        ui, "list_archived_owner_connections", AsyncMock(return_value=()),
    )
    cb = handler(monkeypatch)
    instance = bot()
    await cb(query("archives"), instance)
    actions = buttons(instance.send_message.await_args)
    assert actions.count("own:saasinvoices:0") == 1
    assert "own:projects" in actions


def test_central_native_telegram_menu_exposes_billing_but_customer_does_not():
    from telegram_membership_saas.telegram_menus import commands_for

    for role in ("central", "platform"):
        for language in ("ru", "en"):
            commands = commands_for(role, language)
            assert [c.command for c in commands].count("billing") == 1
            label = next(c.description for c in commands if c.command == "billing")
            assert "Счёт аккаунта" in label if language == "ru" else (
                "Account bill" in label
            )
            assert [c.command for c in commands] == [
                "start", "bots", "billing",
            ]
    for role in ("buyer", "owner", "payment_manager"):
        assert "billing" not in [c.command for c in commands_for(role, "ru")]


async def test_registered_connection_has_rich_explanations_and_colored_two_column_rows(
    monkeypatch,
):
    from telegram_membership_saas.localization import _language

    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id, has_bot=True),
    ))
    for lang, heading, invoices in (
        ("ru", "УПРАВЛЕНИЕ ПОДКЛЮЧЕНИЕМ", "Счёт бота"),
        ("en", "MANAGE CONNECTION", "Bot bill"),
    ):
        locale = _language.set(lang)
        try:
            instance = bot()
            await cb(query("bots"), instance)
            sent = instance.send_message.await_args
            screen = sent.args[1]
            rows = sent.kwargs["reply_markup"].inline_keyboard
            assert heading in screen
            assert invoices in screen
            assert "СОСТОЯНИЕ ЗАПУСКА" in screen if lang == "ru" else (
                "LAUNCH STATUS" in screen
            )
            assert "<b>" in screen and "<i>" in screen
            assert "⚪" in screen  # Pending runtime isn't mislabeled as running.
            assert [len(row) for row in rows] == [2, 2, 2, 1, 1]
            assert [[b.style for b in row] for row in rows] == [
                ["success", "success"],
                ["primary", "primary"],
                ["primary", "primary"],
                ["primary"],
                ["danger"],
            ]
            assert rows[0][0].url == "https://t.me/test_bot"
            assert rows[0][1].callback_data == "own:bots"
            assert rows[1][0].callback_data == "own:connect"
            assert rows[1][1].callback_data.startswith("own:managedsync:")
            assert rows[2][0].callback_data == "own:connrename:" + project_id.hex
            assert rows[2][1].callback_data == (
                "own:saasbotbill:" + encode_uuid(project_id)
            )
            assert rows[3][0].callback_data == "own:projects"
            assert rows[4][0].callback_data.startswith("own:drop:")
            assert project_id.hex not in screen  # No technical IDs in content.
        finally:
            _language.reset(locale)


async def test_botfather_first_connection_shows_only_success_with_manage_choice(
    monkeypatch,
):
    project_id, actor_id = uuid4(), uuid4()
    secret = "123456789:" + "A" * 40
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=actor_id,
    ))
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        "new_bot_token", None, {}, None, "secret-generation",
    )))
    monkeypatch.setattr(ui, "register_new_owner_bot", AsyncMock(
        return_value=(project_id, SimpleNamespace(username="vimi_first_bot")),
    ))
    monkeypatch.setattr(ui, "get_project_import_status", AsyncMock(
        return_value=None,
    ))
    instance = bot()
    instance.delete_message = AsyncMock(return_value=True)
    router = ui.create_owner_setup_router(MagicMock())
    reply = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "owner_reply_handler"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123, first_name="Owner", username="owner"),
        chat=SimpleNamespace(id=123, type="private"),
        text=secret, message_id=200,
        reply_to_message=None,
    )
    await reply(message, instance)
    instance.delete_message.assert_awaited_once_with(123, 200)
    assert instance.send_message.await_count == 1  # no auto-open management
    sent = instance.send_message.await_args
    assert "БОТ УСПЕШНО ПОДКЛЮЧЁН" in sent.args[1]
    assert secret not in sent.args[1]
    rows = sent.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in rows] == [1, 1]
    assert rows[0][0].url == "https://t.me/vimi_first_bot"
    assert rows[0][0].style == "success"
    assert rows[1][0].callback_data == "own:select:" + project_id.hex
    assert rows[1][0].style == "primary"


async def test_central_bot_bill_uses_exact_bot_not_account_and_returns_to_it(
    monkeypatch,
):
    project_id, other_id, invoice_id = uuid4(), uuid4(), uuid4()
    # The currently selected project can be another one because Telegram
    # old inline messages can be pressed after navigation.
    cb = handler(monkeypatch, selected=other_id)
    summary = AsyncMock(return_value={
        "project_id": str(project_id), "invoice_id": str(invoice_id),
        "period_start": "2026-12-16T10:00:00+03:00",
        "period_end": "2027-01-16T10:00:00+03:00",
        "first_period": False,
        "turnover": [
            {"kind": "subscription", "currency": "RUB",
             "sold": 2, "gross": "2000"},
            {"kind": "file", "currency": "RUB",
             "sold": 1, "gross": "1000"},
        ],
        "service_rub": "150", "subscription_rub": "100",
        "file_rub": "50", "total_rub": "300",
        "paid_rub": "0", "credit_rub": "0", "total_due_rub": "300",
    })
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", summary)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id, has_bot=True),
    ))
    monkeypatch.setattr(ui, "saas_price_profile", AsyncMock(
        return_value={"currency": "RUB"},
    ))
    account = AsyncMock(side_effect=AssertionError("Account report used"))
    monkeypatch.setattr(ui, "merchant_saas_statements", account)
    mock_trial = AsyncMock(side_effect=AssertionError("Invoice is already issued"))
    monkeypatch.setattr(ui, "merchant_saas_trial_status", mock_trial)
    mock_preview = AsyncMock(side_effect=AssertionError("Issued invoice is final"))
    monkeypatch.setattr(ui, "merchant_saas_bot_native_sales_preview", mock_preview)
    from telegram_membership_saas.localization import _language
    for lang, caption in (("ru", "СЧЁТ БОТА"), ("en", "BOT BILL")):
        token = _language.set(lang)
        try:
            inst = bot()
            q = query("saasbotbill:" + encode_uuid(project_id))
            await cb(q, inst)
            q.answer.assert_awaited_once()
            sent = inst.send_message.await_args
            assert caption in sent.args[1]
            assert "@test_bot" in sent.args[1]
            assert "300.00 ₽" in sent.args[1]
            assert str(invoice_id) not in sent.args[1]
            rows = sent.kwargs["reply_markup"].inline_keyboard
            assert [len(row) for row in rows] == [1, 1]
            assert rows[0][0].callback_data == (
                "own:spm:" + encode_uuid(invoice_id) + ":RUB:0"
            )
            assert rows[0][0].style == "success"
            assert rows[1][0].callback_data == "own:select:" + project_id.hex
            assert rows[1][0].style == "primary"
        finally:
            _language.reset(token)
    assert summary.await_count == 2
    for call in summary.await_args_list:
        assert call.kwargs["project_id"] == project_id
    mock_trial.assert_not_awaited()
    mock_preview.assert_not_awaited()
    account.assert_not_awaited()


async def test_central_bot_bill_trial_has_only_back_and_readonly_sales(
    monkeypatch,
):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id, has_bot=True),
    ))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", AsyncMock(
        return_value={
            "project_id": str(project_id), "invoice_id": None,
            "total_due_rub": "0", "turnover": [],
            "upcoming_at": "2099-11-24T18:00:00+03:00",
        },
    ))
    monkeypatch.setattr(ui, "saas_price_profile", AsyncMock(
        return_value={"currency": "USD"},
    ))
    trial = AsyncMock(return_value={
        "state": "trial", "remaining_seconds": 57 * 3600,
        "trial_ends_at": "2099-11-24T18:00:00+03:00",
    })
    monkeypatch.setattr(ui, "merchant_saas_trial_status", trial)
    sales = AsyncMock(return_value={
        "month": "2026-11-01",
        "rows": [
            {"kind": "subscription", "sold": 4,
             "currency": "RUB", "gross": "2000"},
            {"kind": "file_stars", "sold": 2,
             "currency": "XTR", "gross": "250"},
            {"kind": "file_crypto", "sold": 1,
             "currency": "TON", "gross": "0.5"},
        ],
    })
    monkeypatch.setattr(ui, "merchant_saas_bot_native_sales_preview", sales)
    inst = bot()
    q = query("saasbotbill:" + encode_uuid(project_id))
    await cb(q, inst)
    q.answer.assert_awaited_once()
    text = inst.send_message.await_args.args[1]
    assert "ПРОБНЫЙ ПЕРИОД" in text
    assert "2000" in text and "250" in text and "TON" in text
    assert "4" in text and "2" in text
    assert "$5" in text
    assert "0 ₽" in text
    markup = inst.send_message.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == (
        "own:select:" + project_id.hex
    )
    assert not any(
        b.callback_data.startswith("own:spm:")
        for row in markup.inline_keyboard for b in row
    )
    assert sales.await_args.kwargs["project_id"] == project_id
    assert trial.await_args.kwargs["project_id"] == project_id


async def test_central_bot_bill_denies_foreign_owner_and_stale_mismatch(
    monkeypatch,
):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    blocked = AsyncMock(side_effect=PermissionError("Not your bot"))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", blocked)
    inst = bot()
    req = query("saasbotbill:" + encode_uuid(project_id))
    await cb(req, inst)
    assert req.answer.await_args.kwargs["show_alert"] is True
    inst.send_message.assert_not_awaited()
    # Even a broken callback result must not display another bot's invoice.
    blocked.side_effect = None
    blocked.return_value = {
        "project_id": str(uuid4()), "invoice_id": None,
        "total_due_rub": "0", "turnover": [],
    }
    req2 = query("saasbotbill:" + encode_uuid(project_id))
    await cb(req2, inst)
    assert req2.answer.await_args.kwargs["show_alert"] is True
    inst.send_message.assert_not_awaited()
