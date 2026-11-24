"""Owner-scoped ViMi Pay statements and payment instructions contracts.

Fake database sessions preserve the real tenant scope and SQL call order
without writing invoices or entering a live PostgreSQL instance.
"""

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_payment_service as payment


def _result(value):
    return SimpleNamespace(scalar_one=lambda: value)


def _session(monkeypatch, *responses, owner=None):
    """Intercept the production tenant context while keeping query values visible."""
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(value) for value in responses]),
        scalar=AsyncMock(return_value=owner),
    )
    contexts = []

    @asynccontextmanager
    async def scope(_factory, context):
        contexts.append(context)
        yield session

    monkeypatch.setattr(payment, "tenant_session_scope", scope)
    return session, contexts


def _invoice_report(**changes):
    report = {
        "total": 0,
        "unpaid": 0,
        "outstanding_rub": "0.00",
        "details_configured": False,
        "instructions": None,
        "invoices": [],
    }
    report.update(changes)
    return report


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(payment, "_check_actor", lambda *_: None)


async def test_platform_payment_details_rejects_invalid_sql_response(
    monkeypatch, operator,
):
    session, scopes = _session(monkeypatch, ["not a dict"])
    actor = uuid4()
    with pytest.raises(RuntimeError, match="platform payment instructions"):
        await payment.platform_saas_payment_details(
            object(), actor_user_id=actor, actor_telegram_id=500,
        )
    assert scopes[0].user_id == actor
    assert session.execute.await_count == 1


async def test_payment_instructions_are_read_only_with_operator_scope(
    monkeypatch, operator,
):
    actor = uuid4()
    expected = {"instructions": "Transfer to verified bank account"}
    session, scopes = _session(monkeypatch, expected)

    actual = await payment.platform_saas_payment_details(
        object(), actor_user_id=actor, actor_telegram_id=500,
    )

    assert actual == expected
    assert scopes[0].user_id == actor
    assert "platform_saas_payment_details_read" in str(
        session.execute.await_args.args[0]
    )


@pytest.mark.parametrize(
    "invalid",
    ["short", "       ", "X" * 901, "Valid bank details\x00hidden"],
)
async def test_payment_instructions_refuse_invalid_or_control_characters(
    monkeypatch, operator, invalid,
):
    called = AsyncMock()
    monkeypatch.setattr(payment, "tenant_session_scope", called)
    with pytest.raises(ValueError):
        await payment.configure_platform_saas_payment(
            object(), actor_user_id=uuid4(), actor_telegram_id=500,
            instructions=invalid,
        )
    called.assert_not_awaited()


@pytest.mark.parametrize("instructions", [None, "  Transfer to bank account  "])
async def test_payment_instructions_preserve_none_or_trimmed_value(
    monkeypatch, operator, instructions,
):
    session, _ = _session(monkeypatch, True)
    await payment.configure_platform_saas_payment(
        object(), actor_user_id=uuid4(), actor_telegram_id=500,
        instructions=instructions,
    )
    assert session.execute.await_args.args[1] == {
        "instructions": instructions.strip() if instructions else None,
    }


async def test_payment_instructions_require_db_acknowledgement(monkeypatch, operator):
    _session(monkeypatch, False)
    with pytest.raises(RuntimeError, match="configuration unavailable"):
        await payment.configure_platform_saas_payment(
            object(), actor_user_id=uuid4(), actor_telegram_id=500,
            instructions="Transfer to verified account",
        )


@pytest.mark.parametrize(
    ("func", "parameters"),
    [
        ("merchant_saas_unified_monthly_preview", {}),
        ("merchant_saas_project_preview", {"project_id": uuid4()}),
        ("merchant_saas_bot_native_sales_preview", {"project_id": uuid4()}),
        ("merchant_saas_bill_summary", {}),
    ],
)
async def test_monthly_reports_reject_nonfirst_day_without_database_access(
    monkeypatch, func, parameters,
):
    called = AsyncMock()
    monkeypatch.setattr(payment, "tenant_session_scope", called)
    with pytest.raises(ValueError, match="month"):
        await getattr(payment, func)(
            object(), actor_user_id=uuid4(),
            month=date(2026, 11, 15), **parameters,
        )
    called.assert_not_awaited()


@pytest.mark.parametrize(
    ("func", "bad", "message"),
    [
        ("merchant_saas_unified_monthly_preview", {"rails": None}, "commission preview"),
        ("merchant_saas_project_preview", {"rows": "not-list"}, "account preview"),
        ("merchant_saas_bot_native_sales_preview", {}, "native sales preview"),
        ("merchant_saas_bill_summary", {"turnover": None}, "unified bill"),
    ],
)
async def test_monthly_reports_fail_closed_on_incomplete_snapshot(
    monkeypatch, func, bad, message,
):
    project_id = uuid4()
    actor = uuid4()
    _session(monkeypatch, bad, owner=actor)
    params = {"month": date(2026, 11, 1), "actor_user_id": actor}
    if func in (
        "merchant_saas_project_preview",
        "merchant_saas_bot_native_sales_preview",
    ):
        params["project_id"] = project_id
    with pytest.raises(RuntimeError, match=message):
        await getattr(payment, func)(object(), **params)


@pytest.mark.parametrize(
    "func",
    ["merchant_saas_unified_monthly_preview", "merchant_saas_trial_status"],
)
async def test_current_owner_check_denies_transferred_project(
    monkeypatch, func,
):
    actor = uuid4()
    project = uuid4()
    session, _ = _session(monkeypatch, owner=uuid4())
    call = getattr(payment, func)
    parameters = (
        {"month": date(2026, 11, 1)} if func == "merchant_saas_unified_monthly_preview"
        else {}
    )
    with pytest.raises(PermissionError, match="Only project owner"):
        await call(
            object(), actor_user_id=actor, project_id=project, **parameters,
        )
    session.execute.assert_not_awaited()


async def test_trial_report_requires_structured_state_and_owner_scope(monkeypatch):
    actor = uuid4()
    project = uuid4()
    session, scopes = _session(monkeypatch, {"trial": "true"}, owner=actor)
    with pytest.raises(RuntimeError, match="trial state"):
        await payment.merchant_saas_trial_status(
            object(), actor_user_id=actor, project_id=project,
        )
    assert scopes[0].user_id == actor
    assert scopes[0].project_id == project
    assert session.scalar.await_count == 1


async def test_trial_report_preserves_authoritative_state(monkeypatch):
    actor = uuid4()
    expected = {"state": "active", "remaining_hours": 23}
    _session(monkeypatch, expected)
    result = await payment.merchant_saas_trial_status(
        object(), actor_user_id=actor,
    )
    assert result == expected


@pytest.mark.parametrize("page", [-1, 100001])
async def test_statement_pagination_is_bounded_before_database_access(
    monkeypatch, page,
):
    scope = AsyncMock()
    monkeypatch.setattr(payment, "tenant_session_scope", scope)
    with pytest.raises(ValueError, match="billing page"):
        await payment.merchant_saas_statements(
            object(), actor_user_id=uuid4(), page=page,
        )
    scope.assert_not_awaited()


async def test_statements_never_expose_transferred_owner_liability(monkeypatch):
    actor = uuid4()
    session, contexts = _session(monkeypatch, owner=uuid4())
    with pytest.raises(PermissionError, match="actual project owner"):
        await payment.merchant_saas_statements(
            object(), actor_user_id=actor, project_id=uuid4(),
        )
    assert contexts[0].user_id == actor
    session.execute.assert_not_awaited()


async def test_statements_keep_original_invoice_and_verified_credit_snapshots(
    monkeypatch,
):
    actor = uuid4()
    project = uuid4()
    invoice_id = uuid4()
    data = _invoice_report(
        total=1, unpaid=1, outstanding_rub="150.00",
        details_configured=True,
        instructions="Bank transfer details",
        invoices=[{"id": str(invoice_id), "status": "issued"}],
        due_now_count=1, due_now_rub="150.00",
        upcoming_count=0, upcoming_rub="0",
    )
    credits = {"total_credited_rub": "20.00", "credit_count": 2}
    settlement = {"currency": "RUB", "amount": "150.00"}
    session, contexts = _session(
        monkeypatch, data, credits, 3, settlement, owner=actor,
    )
    # Snapshot retrieval follows invoice listing before credit and foreign counts;
    # using side effects in exact SQL execution order protects attribution.
    session.execute.side_effect = [
        _result(data),
        _result(credits),
        _result(3),
        _result(settlement),
    ]

    result = await payment.merchant_saas_statements(
        object(), actor_user_id=actor, project_id=project, page=2,
    )

    assert result.total == 1
    assert result.unpaid == 1
    assert result.outstanding_rub == Decimal("150.00")
    assert result.pending_credit_rub == Decimal("20.00")
    assert result.pending_credit_count == 2
    assert result.foreign_unpaid_count == 3
    assert result.due_now_rub == Decimal("150.00")
    assert result.invoices == ({"id": str(invoice_id), "status": "issued",
                               "settlement": settlement},)
    assert result.page == 2
    assert contexts[0].user_id == actor
    assert contexts[0].project_id == project
    calls = session.execute.await_args_list
    assert len(calls) == 4
    assert calls[0].args[1] == {"page": 2}
    assert calls[1].args[1] == {"owner": actor}
    assert calls[3].args[1] == {"invoice": invoice_id}


@pytest.mark.parametrize(
    ("report", "credits", "settlement", "match"),
    [
        (_invoice_report(invoices=[]), "invalid", None, "merchant credit report"),
        (_invoice_report(invoices=[{"id": str(uuid4())}]), {
          "total_credited_rub": "0", "credit_count": 0},
          "bad settlement", "invoice currency snapshot"),
    ],
)
async def test_statement_reports_reject_invalid_credit_or_invoice_evidence(
    monkeypatch, report, credits, settlement, match,
):
    responses = [report, credits, 0]
    if settlement is not None:
        responses.append(settlement)
    _session(monkeypatch, *responses)
    with pytest.raises(RuntimeError, match=match):
        await payment.merchant_saas_statements(
            object(), actor_user_id=uuid4(),
        )
