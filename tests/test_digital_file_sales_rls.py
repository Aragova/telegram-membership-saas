"""Real PostgreSQL: file purchases must not leak media or mutate paid records."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots.models import BotInstance
from telegram_membership_saas.modules.file_sales import service as svc
from telegram_membership_saas.modules.file_sales.models import FilePurchase
from telegram_membership_saas.modules.identity.models import ProjectCustomer
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


async def test_file_sale_catalog_paid_gate_no_cross_project_leaks():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, foreign, cid, _, buyer_tid = (
            await setup_subscriber(factory)
        )
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="file_sales_test", display_name="File sales",
                token_ciphertext="test-token-encrypted",
            ))
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.add_folder(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, name="Stolen",
            )
        folder = await svc.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Ebooks",
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.create_product(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, folder_id=folder.id,
                name="Stolen", stars=5,
            )
        product = await svc.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id,
            name="Guide", stars=15,
        )
        assert await svc.list_folders(
            factory, project_id=project, bot_id=bot_id,
        ) == ()
        with pytest.raises((PermissionError, ValueError)):
            await svc.view_product(
                factory, project_id=project, bot_id=bot_id,
                product_id=product.id,
            )
        await svc.add_product_asset(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id,
            kind="document", file_id="TELEGRAM_PRIVATE_DOC_ID",
        )
        await svc.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id,
            status="published",
        )
        assert (await svc.list_folders(
            factory, project_id=project, bot_id=bot_id,
        ))[0].id == folder.id
        with pytest.raises(ValueError):
            await svc.add_product_asset(
                factory, project_id=project, bot_id=bot_id,
                actor_id=owner, product_id=product.id,
                kind="document", file_id="CANNOT_REPLACE_AFTER_PUBLICATION",
            )
        first = await svc.create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=product.id,
        )
        again = await svc.create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=product.id,
        )
        assert not first.paid and again.id == first.id
        assert not await svc.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=15, currency="RUB",
        )
        assert not await svc.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=14, currency="XTR",
        )
        assert not await svc.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=tg_id(), invoice_payload=first.invoice_payload,
            amount=15, currency="XTR",
        )
        assert await svc.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=15, currency="XTR",
        )
        with pytest.raises(PermissionError):
            await svc.purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, purchase_id=first.id,
            )
        with pytest.raises((ValueError, PermissionError)):
            await svc.confirm_stars_payment(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
                amount=15, currency="USD", charge_id="invalid",
            )
        paid = await svc.confirm_stars_payment(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=15, currency="XTR", charge_id="test_" + uuid4().hex,
        )
        repeat = await svc.confirm_stars_payment(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=15, currency="XTR", charge_id=paid.telegram_charge_id,
        )
        assert repeat.id == paid.id
        with pytest.raises(PermissionError):
            await svc.confirm_stars_payment(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
                amount=15, currency="XTR", charge_id="different-charge",
            )
        one = await svc.create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=product.id,
        )
        assert one.paid and one.id == paid.id
        with pytest.raises(PermissionError):
            await svc.purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=tg_id(), purchase_id=paid.id,
            )
        purchase, media = await svc.purchased_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, purchase_id=paid.id,
        )
        assert purchase.id == first.id
        assert media[0].file_id == "TELEGRAM_PRIVATE_DOC_ID"

        # Remove from sale without taking paid customer's permanent redownload.
        await svc.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, status="archived",
        )
        assert not await svc.precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=first.invoice_payload,
            amount=15, currency="XTR",
        )
        assert (await svc.purchased_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, purchase_id=paid.id,
        ))[1][0].file_id == "TELEGRAM_PRIVATE_DOC_ID"
        with pytest.raises((ValueError, PermissionError, TenantAccessDenied)):
            await svc.view_product(
                factory, project_id=foreign, bot_id=bot_id,
                product_id=product.id, actor_id=outsider,
            )
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "UPDATE file_sale_purchases SET stars_amount=1 WHERE id=:pid"
                ), {"pid": paid.id})
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "DELETE FROM file_sale_purchases WHERE id=:pid"
                ), {"pid": paid.id})
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            row = await session.scalar(select(FilePurchase).where(
                FilePurchase.id == paid.id,
            ))
            assert row.telegram_charge_id == paid.telegram_charge_id
            assert row.stars_amount == 15
            buyer = await session.scalar(select(ProjectCustomer).where(
                ProjectCustomer.id == cid,
            ))
            buyer.is_banned = True
        with pytest.raises(PermissionError):
            await svc.create_stars_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, product_id=product.id,
            )
    finally:
        await engine.dispose()


async def test_paid_orders_survive_an_archived_product_in_buyer_history():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="file_history_test", display_name="File history",
                token_ciphertext="test-token",
            ))
        folder = await svc.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Archive",
        )
        product = await svc.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id, name="PDF", stars=1,
        )
        await svc.add_product_asset(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id,
            kind="document", file_id="source",
        )
        await svc.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, status="published",
        )
        order = await svc.create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=product.id,
        )
        await svc.confirm_stars_payment(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, invoice_payload=order.invoice_payload,
            amount=1, currency="XTR", charge_id=uuid4().hex,
        )
        await svc.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=product.id, status="archived",
        )
        history = await svc.list_purchases(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_tid,
        )
        assert history[0].product_name == "PDF"
        await svc.mark_file_delivery(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, purchase_id=order.id,
        )
    finally:
        await engine.dispose()
