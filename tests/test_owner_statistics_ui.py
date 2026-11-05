"""Statistics navigation must stay on connected owner bots only."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.statistics import (
    OwnerStatistics,
    _period_start,
)


def _bot():
    bot = MagicMock()
    bot.id = 333333
    bot.send_message = AsyncMock()
    return bot


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(id=54321, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )


def _handler(project):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        handler.callback for handler in router.callback_query.handlers
        if handler.callback.__name__ == "owner_callback"
    )


def _stats(period):
    return OwnerStatistics(
        project_name="<Channel>", timezone="Europe/Moscow",
        period=period, since=date(2026, 11, 1), through=date(2026, 11, 5),
        payment_count=2, currency_totals=(("RUB", Decimal("550.25"), 1),
                                          ("USD", Decimal("10"), 1)),
        total_customers=5, new_customers=3, active_paid_customers=1,
        expiring_paid_customers=1, active_manual_grants=2, waiting_review=4,
    )


@pytest.mark.parametrize("period", ["7", "30", "all"])
async def test_statistics_screen_from_owner_callback(monkeypatch, period):
    project, actor = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    spy = AsyncMock(return_value=_stats(period))
    monkeypatch.setattr(ui, "read_owner_statistics", spy)
    bot, query = _bot(), _query("statistics:" + period)
    await _handler(project)(query, bot)
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["project_id"] == project
    assert spy.await_args.kwargs["actor_id"] == actor
    assert spy.await_args.kwargs["period"] == period
    text = bot.send_message.await_args.args[1]
    assert "&lt;Channel&gt;" in text
    assert "550.25" in text and "USD: 10" in text and "RUB" in text
    assert "ViMi Pay" in text
    buttons = [
        b.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert {"own:statistics:7", "own:statistics:30",
            "own:statistics:all", "own:dashboard"} <= set(buttons)
    assert max(map(len, buttons)) <= 64


async def test_statistics_denied_in_central_and_for_nonmanagers(monkeypatch):
    project = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    bot = _bot()
    q1 = _query("statistics:all")
    await _handler(project)(q1, bot)
    bot.send_message.assert_not_awaited()
    assert q1.answer.await_args.kwargs["show_alert"]
    central_router = ui.create_owner_setup_router(MagicMock())
    central = next(
        h.callback for h in central_router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    q2 = _query("statistics:7")
    await central(q2, bot)
    bot.send_message.assert_not_awaited()
    assert q2.answer.await_args.kwargs["show_alert"]


def test_calendar_days_are_local_and_period_is_valid():
    from datetime import UTC, datetime
    moment = datetime(2026, 11, 5, 0, 30, tzinfo=UTC)
    cutoff, start, end = _period_start(
        "7", now=moment, timezone="Europe/Moscow",
    )
    assert start == date(2026, 10, 30)
    assert end == date(2026, 11, 5)
    assert cutoff.isoformat() == "2026-10-29T21:00:00+00:00"
    with pytest.raises(ValueError):
        _period_start("bad", now=moment, timezone="Europe/Moscow")


def test_statistics_amount_keeps_integer_trailing_zeroes():
    assert ui._statistics_amount(Decimal("100")) == "100"
    assert ui._statistics_amount(Decimal("10.000000")) == "10"
    assert ui._statistics_amount(Decimal("100.50000")) == "100.5"
    assert ui._statistics_amount(Decimal("0.00000")) == "0"
