"""A charged-but-unrecorded buyer can recover from authentic Bot API records."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.file_sales import recovery as svc


async def test_bot_ledger_recovery_requires_payer_payload_and_amount(monkeypatch):
    project = uuid4()
    buyer_id = 775533
    payload = "fs:" + uuid4().hex
    pending = SimpleNamespace(
        id=uuid4(), invoice_payload=payload, stars_amount=99,
    )
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [pending]))

    @asynccontextmanager
    async def scoped(factory, scoped_project):
        assert scoped_project == project
        yield session

    monkeypatch.setattr(svc, "system_project_session_scope", scoped)
    verified = AsyncMock(return_value=SimpleNamespace(id=pending.id))
    monkeypatch.setattr(svc, "confirm_stars_payment", verified)
    valid = SimpleNamespace(
        id="charge_abc", amount=99,
        source=SimpleNamespace(
            type="user", transaction_type="invoice_payment",
            user=SimpleNamespace(id=buyer_id),
            invoice_payload=payload,
        ),
    )
    invalid = [
        SimpleNamespace(
            id="wrong_user", amount=99,
            source=SimpleNamespace(
                type="user", transaction_type="invoice_payment",
                user=SimpleNamespace(id=buyer_id + 1),
                invoice_payload=payload,
            ),
        ),
        SimpleNamespace(
            id="wrong_amount", amount=98,
            source=SimpleNamespace(
                type="user", transaction_type="invoice_payment",
                user=SimpleNamespace(id=buyer_id),
                invoice_payload=payload,
            ),
        ),
        SimpleNamespace(
            id="wrong_kind", amount=99,
            source=SimpleNamespace(
                type="user", transaction_type="gift_purchase",
                user=SimpleNamespace(id=buyer_id),
                invoice_payload=payload,
            ),
        ),
    ]
    bot = SimpleNamespace(
        id=9988,
        get_star_transactions=AsyncMock(return_value=SimpleNamespace(
            transactions=[*invalid, valid],
        )),
    )
    recovered = await svc.recover_buyer_stars(
        MagicMock(), bot=bot, project_id=project, buyer_id=buyer_id,
    )
    assert recovered == (pending.id,)
    assert verified.await_count == 1
    assert verified.await_args.kwargs == {
        "project_id": project, "bot_id": 9988, "buyer_id": buyer_id,
        "invoice_payload": payload, "amount": 99, "currency": "XTR",
        "charge_id": "charge_abc",
    }
    assert bot.get_star_transactions.await_args.kwargs == {
        "offset": 0, "limit": 100,
    }


async def test_no_pending_orders_does_not_scan_bot_ledger(monkeypatch):
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: ()))

    @asynccontextmanager
    async def scoped(factory, scoped_project):
        yield session

    monkeypatch.setattr(svc, "system_project_session_scope", scoped)
    bot = SimpleNamespace(id=9988, get_star_transactions=AsyncMock())
    assert await svc.recover_buyer_stars(
        MagicMock(), bot=bot, project_id=uuid4(), buyer_id=22,
    ) == ()
    bot.get_star_transactions.assert_not_awaited()
