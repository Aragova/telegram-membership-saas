"""Operator-only verification of original Crypto Pay merchant transfer credits.

This module is read-only against the provider: NO payout/transfer creation.
It cannot change customer fulfillment, monthly invoices or SaaS holds.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    verify_completed_transfer,
)
from telegram_membership_saas.modules.crypto_pay.service import load_crypto_app_token
from telegram_membership_saas.modules.platform_saas_billing_service import _check_actor
from telegram_membership_saas.tenant_context import TenantContext


async def verify_and_record_crypto_refund_transfer(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, actor_telegram_id: int,
    sale_id: UUID, spend_id: str, encryption_key: SecretStr | None,
    settings: Settings | None = None,
    client_factory: type[CryptoPayClient] = CryptoPayClient,
) -> UUID:
    """Trust only original merchant getTransfers and a frozen invoice label.

    An external actor must already have transferred money deliberately with
    a UUID-labelled spend_id. The platform NEVER initiates the payout.
    A lost response can be safely retried without crediting twice.
    """
    _check_actor(actor_telegram_id, settings)
    if encryption_key is None:
        raise CryptoPayError("Crypto Pay credentials are unavailable")
    if not isinstance(spend_id, str) or len(spend_id) > 100:
        raise ValueError("Invalid Crypto Pay refund spend ID")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        candidate = (await session.execute(text(
            "SELECT public.platform_saas_crypto_refund_candidate("
            ":sale,:spend) AS data"
        ).columns(data=JSONB), {
            "sale": sale_id, "spend": spend_id,
        })).scalar_one()
    if not isinstance(candidate, dict):
        raise CryptoPayError("Crypto Pay refund source is unavailable")

    project_id = UUID(candidate["project_id"])
    app_id = UUID(candidate["app_id"])
    buyer_id = int(candidate["buyer_id"])
    asset = candidate["asset"]
    async with system_project_session_scope(factory, project_id) as session:
        app, token = await load_crypto_app_token(
            session, project_id=project_id, app_id=app_id,
            encryption_key=encryption_key,
        )
        if app.environment != "mainnet" or app.telegram_bot_id != int(
            candidate["bot_id"]
        ):
            raise CryptoPayError("Original crypto merchant app mismatch")

    # Provider I/O is outside both transactions. No API secrets in logs.
    async with client_factory(token, testnet=False) as provider:
        transfers = await provider.get_transfers(spend_id=spend_id)
    if len(transfers) != 1:
        raise CryptoPayError("Completed Crypto Pay refund transfer is missing")
    verified = verify_completed_transfer(
        transfers[0], spend_id=spend_id,
        buyer_id=buyer_id, asset_code=asset,
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(text(
            "SELECT public.platform_saas_record_crypto_transfer_credit("
            ":sale,:transfer,:spend,:recipient,:asset,:amount,:finished)"
        ), {
            "sale": sale_id, "transfer": verified.transfer_id,
            "spend": verified.spend_id, "recipient": verified.buyer_id,
            "asset": verified.asset_code, "amount": verified.amount,
            "finished": verified.completed_at,
        })).scalar_one()
