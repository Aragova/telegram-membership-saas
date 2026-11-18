"""Isolated Telegram MTProto revenue reader: verified wire contract and UI."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from telegram_membership_saas import channel_finance_agent as agent
from telegram_membership_saas.modules.owner_setup import channel_revenue_bridge as bridge
from telegram_membership_saas.modules.owner_setup.channel_revenue_bridge import (
    ChannelRevenue,
    parse_channel_revenue,
)
from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
    ChannelFinance,
    ChannelOwner,
    DashboardFinances,
    _revenue_actor_matches_creator,
)
from telegram_membership_saas.modules.owner_setup.dashboard_ui import (
    classic_dashboard,
    rich_dashboard,
)


def _snapshot(**overrides):
    value = dict(
        chat_id=-100777777, owner_id=12345,
        current_balance="5400", available_balance="3200",
        withdrawal_enabled=True, next_withdrawal_at=None,
        observed_at=datetime.now(UTC).isoformat(),
    )
    value.update(overrides)
    return value


def test_authoritative_channel_revenue_contract_preserves_exact_stars():
    now = datetime.now(UTC)
    res = parse_channel_revenue(
        _snapshot(
            current_balance="5400.000000001",
            available_balance="3200",
            next_withdrawal_at=int((now + timedelta(hours=1)).timestamp()),
            observed_at=now.isoformat(),
        ),
        chat_id=-100777777, owner_id=12345, now=now,
    )
    assert res.current_balance == Decimal("5400.000000001")
    assert res.available_balance == Decimal("3200")
    assert res.withdrawal_enabled is True
    assert res.next_withdrawal_at > now


@pytest.mark.parametrize("override", [
    {"chat_id": -100888888},
    {"owner_id": 98765},
    {"current_balance": "-1"},
    {"current_balance": "NaN"},
    {"available_balance": "5401"},
    {"withdrawal_enabled": "true"},
    {"observed_at": (datetime.now(UTC) - timedelta(minutes=4)).isoformat()},
    {"observed_at": (datetime.now(UTC) + timedelta(days=3)).isoformat()},
    {"next_withdrawal_at": -50},
])
def test_mismatched_or_stale_external_data_fail_closed(override):
    with pytest.raises(ValueError):
        parse_channel_revenue(
            _snapshot(**override), chat_id=-100777777, owner_id=12345,
        )


def test_agent_normalizes_real_tl_starsamount_and_status():
    status = SimpleNamespace(
        current_balance=SimpleNamespace(amount=5400, nanos=1),
        available_balance=SimpleNamespace(amount=3200, nanos=0),
        withdrawal_enabled=False, next_withdrawal_at=1_800_000_000,
    )
    raw = agent.normalize_telegram_revenue(
        status, chat_id=-100777777, owner_id=12345,
    )
    assert raw["current_balance"] == "5400.000000001"
    assert raw["available_balance"] == "3200"
    assert raw["withdrawal_enabled"] is False
    assert raw["next_withdrawal_at"] == 1_800_000_000
    assert parse_channel_revenue(
        raw, chat_id=-100777777, owner_id=12345,
    ).current_balance == Decimal("5400.000000001")


def test_real_zero_stars_is_not_confused_with_unavailable():
    valid = agent.normalize_telegram_revenue(
        SimpleNamespace(
            current_balance=SimpleNamespace(amount=0, nanos=0),
            available_balance=SimpleNamespace(amount=0, nanos=0),
            withdrawal_enabled=False, next_withdrawal_at=None,
        ),
        chat_id=-100777777, owner_id=12345,
    )
    revenue = parse_channel_revenue(
        valid, chat_id=-100777777, owner_id=12345,
    )
    assert revenue.current_balance == Decimal(0)
    assert revenue.available_balance == Decimal(0)


def _render_data(revenue):
    return dict(
        project_name="@premium0fans_bot", timezone="Europe/Moscow",
        trial_banner="", sales_status="✅ Продажи доступны",
        readiness=(
            ("Канал / группа", True), ("Тариф", True),
            ("Оплата", True), ("Бот работает", True),
        ),
        finances=DashboardFinances(
            bot_stars="0",
            channels=(ChannelFinance(
                name="VIP", chat_id="-100777777",
                owner_label="@owner · ID 12345", owner_deleted=False,
                stars_recipient="channel", owner_username="owner",
                owner_user_id=12345, revenue=revenue,
            ),),
        ),
    )


def test_both_ru_renderers_show_real_balance_withdraw_and_waiting_date():
    now = datetime.now(UTC)
    revenue = ChannelRevenue(
        current_balance=Decimal("5400"),
        available_balance=Decimal("3200"),
        withdrawal_enabled=False,
        next_withdrawal_at=now + timedelta(days=1),
        observed_at=now,
    )
    for renderer in (classic_dashboard, rich_dashboard):
        output = renderer(**_render_data(revenue))
        for substring in ("5400", "3200", "Ожидание", "Ожидание до", "Проверено"):
            assert substring in output
        assert "@owner</a>" in output
    assert "Недоступен через Bot API" not in rich_dashboard(**_render_data(revenue))


def test_withdrawal_enabled_but_no_available_stars_is_not_success():
    status = ChannelRevenue(
        current_balance=Decimal("0"),
        available_balance=Decimal("0"),
        withdrawal_enabled=True,
        next_withdrawal_at=None,
        observed_at=datetime.now(UTC),
    )
    html = rich_dashboard(**_render_data(status))
    assert "Нет доступных звёзд" in html
    assert "✅ Разрешён Telegram" not in html


def test_no_credentials_means_no_telegram_bridge_network_call(monkeypatch):
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(
        channel_finance_bridge_url=None,
        channel_finance_bridge_token_file=None,
    ))

    async def run():
        assert await bridge.live_channel_revenue(
            chat_id=-100777777, owner_id=12345,
        ) is None
    import asyncio
    asyncio.run(run())


@pytest.mark.asyncio
async def test_private_bridge_checks_owner_channel_and_fresh_snapshot(
    monkeypatch, tmp_path,
):
    token_file = tmp_path / "token"
    token_file.write_text("x" * 40, encoding="ascii")
    monkeypatch.setattr(bridge, "get_settings", lambda: SimpleNamespace(
        channel_finance_bridge_url="http://channel-finance:8033",
        channel_finance_bridge_token_file=str(token_file),
    ))
    seen = []
    def fake_request(request):
        seen.append(request)
        return httpx.Response(200, json=_snapshot())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fake_request), trust_env=False,
    ) as client:
        value = await bridge.live_channel_revenue(
            chat_id=-100777777, owner_id=12345, client=client,
        )
        assert value is not None and value.current_balance == Decimal("5400")
        assert len(seen) == 1
        assert seen[0].headers["x-vimi-finance-key"] == "x" * 40
        assert seen[0].url.params["owner_id"] == "12345"

        assert await bridge.live_channel_revenue(
            chat_id=-100777778, owner_id=12345, client=client,
        ) is None


@pytest.mark.asyncio
async def test_reader_does_not_request_unlisted_or_other_users_channels():
    settings = dict(
        owner_id=12345, allowed=frozenset({-100777777}),
        token="x" * 40, api_id=11, api_hash="z" * 32,
        session="s" * 70,
    )
    reader = agent.ChannelRevenueReader(settings)
    reader._telegram_read = AsyncMock(return_value=_snapshot())
    with pytest.raises(PermissionError):
        await reader.read(-100999999, 12345)
    with pytest.raises(PermissionError):
        await reader.read(-100777777, 98765)
    reader._telegram_read.assert_not_awaited()
    first = await reader.read(-100777777, 12345)
    second = await reader.read(-100777777, 12345)
    assert first == second
    reader._telegram_read.assert_awaited_once()


def test_reader_endpoint_never_exposes_values_without_key():
    data = dict(
        owner_id=12345, allowed=frozenset({-100777777}),
        token="x" * 40, session="s" * 70, api_id=11, api_hash="z" * 32,
    )
    reader = agent.ChannelRevenueReader(data)
    reader.read = AsyncMock(return_value=_snapshot())
    agent.app.state.finance_reader = reader
    # Avoid running the real lifespan in tests: startup requires a user's
    # out-of-band authorized Telegram session, which is intentionally absent.
    client = TestClient(agent.app)
    denied = client.get(
        "/v1/channel/-100777777", params={"owner_id": 12345},
    )
    assert denied.status_code == 403
    mismatched = client.get(
        "/v1/channel/-100777777",
        params={"owner_id": 98765},
        headers={"x-vimi-finance-key": "x" * 40},
    )
    assert mismatched.status_code == 404
    allowed = client.get(
        "/v1/channel/-100777777",
        params={"owner_id": 12345},
        headers={"x-vimi-finance-key": "x" * 40},
    )
    assert allowed.status_code == 200
    assert allowed.json()["current_balance"] == "5400"
    reader.read.assert_awaited_once()


@pytest.mark.parametrize(("actor", "creator", "deleted", "allowed"), [
    (12345, 12345, False, True),
    (98765, 12345, False, False),  # project owner may be channel admin
    (None, 12345, False, False),
    (12345, 12345, True, False),  # revoked/deleted owner is not verified
    (12345, None, False, False),
])
def test_sensitive_revenue_requires_actor_telegram_id_to_match_creator(
    actor, creator, deleted, allowed,
):
    owner = ChannelOwner(
        label="@creator", deleted=deleted, user_id=creator,
    )
    assert _revenue_actor_matches_creator(actor, owner) is allowed
