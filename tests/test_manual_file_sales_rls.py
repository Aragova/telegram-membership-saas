"""Manual digital-file checkout: real PostgreSQL RLS, proof and reviewer gating."""
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine, build_session_factory, tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales import manual_service as manual
from telegram_membership_saas.modules.file_sales import service as files
from telegram_membership_saas.modules.file_sales.manual_models import FileManualOrder
from telegram_membership_saas.modules.payments import PaymentMethod, PaymentMethodCurrency
from telegram_membership_saas.modules.pricing import CurrencyAsset
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


async def test_manual_file_checkout_freezes_price_and_requires_owner_approval():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, buyer, outsider, project, _, _, _, buyer_tid = (
            await setup_subscriber(factory)
        )
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="manual_file_test", display_name="Manual files",
                token_ciphertext="encrypted-test-token",
            ))
            rub = await session.get(CurrencyAsset, "RUB")
            assert rub is not None
            method = PaymentMethod(
                project_id=project, name="Банк", provider="manual_settlement",
                instruction_template="Оплатите {amount} {currency} за {plan}",
                status="active",
            )
            session.add(method)
            await session.flush()
            session.add(PaymentMethodCurrency(
                payment_method_id=method.id, project_id=project,
                currency_code="RUB",
            ))
            method_id = method.id

        folder = await files.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="ВИДЕО",
        )
        product = await files.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id,
            name="Видео 720p", stars=None,
        )
        await files.add_product_asset(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            product_id=product.id, kind="document", file_id="private-file",
        )
        with pytest.raises((PermissionError, ValueError)):
            await manual.set_manual_price(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, product_id=product.id,
                currency="RUB", value="1",
            )
        await manual.set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id,
            currency="RUB", value="525",
        )
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, status="published",
        )
        offers = await manual.available_methods(
            factory, project_id=project, bot_id=bot_id,
            product_id=product.id, buyer_id=buyer_tid,
        )
        assert len(offers) == 1 and offers[0].currency == "RUB"
        assert offers[0].amount == Decimal("525")

        order, paid = await manual.create_manual_order(
            factory, project_id=project, bot_id=bot_id,
            product_id=product.id, buyer_id=buyer_tid,
            currency="RUB", method_id=method_id,
        )
        assert not paid and order.status == "pending"
        again, _ = await manual.create_manual_order(
            factory, project_id=project, bot_id=bot_id,
            product_id=product.id, buyer_id=buyer_tid,
            currency="RUB", method_id=method_id,
        )
        assert again.id == order.id
        assert "525 RUB" in order.instruction

        await manual.set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id,
            currency="RUB", value="999",
        )
        with pytest.raises(PermissionError):
            await files.purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, purchase_id=order.id,
            )
        with pytest.raises(ValueError):
            await manual.review_manual_order(
                factory, project_id=project, bot_id=bot_id,
                actor_id=owner, order_id=order.id, approve=True,
            )
        submitted, reviewers = await manual.submit_manual_receipt(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, order_id=order.id,
            kind="photo", file_id="receipt-photo",
        )
        assert submitted.status == "submitted"
        assert reviewers
        with pytest.raises((PermissionError, ValueError)):
            await manual.review_manual_order(
                factory, project_id=project, bot_id=bot_id,
                actor_id=buyer, order_id=order.id, approve=True,
            )
        with pytest.raises(PermissionError):
            await files.purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=tg_id(), purchase_id=order.id,
            )
        approved, changed = await manual.review_manual_order(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, order_id=order.id, approve=True,
        )
        assert changed and approved.status == "approved"
        assert approved.amount == Decimal("525")
        repeated, changed = await manual.review_manual_order(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, order_id=order.id, approve=True,
        )
        assert repeated.id == approved.id and not changed
        one, media = await files.purchased_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, purchase_id=order.id,
        )
        assert one.id == order.id and media[0].file_id == "private-file"
        total, history = await files.list_purchases_page(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_tid,
        )
        assert total == 1 and history[0].id == order.id

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            subs = (await session.scalars(select(Subscription).where(
                Subscription.project_id == project,
            ))).all()
            assert not subs
            stored = await session.scalar(select(FileManualOrder).where(
                FileManualOrder.id == order.id,
            ))
            assert stored.amount == Decimal("525")
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "UPDATE file_sale_manual_orders SET amount=1 WHERE id=:id"
                ), {"id": order.id})
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "DELETE FROM file_sale_manual_orders WHERE id=:id"
                ), {"id": order.id})
    finally:
        await engine.dispose()
