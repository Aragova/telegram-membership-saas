"""Owner deletion is a safe tenant-scoped operation, not payment-history erasure."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales import service as svc
from telegram_membership_saas.modules.file_sales.manual_models import (
    FileManualOrder,
    FileManualPrice,
)
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileProduct,
)
from telegram_membership_saas.modules.payments import PaymentMethod, PaymentMethodCurrency
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


async def _register_bot(factory, *, project_id, owner_id, bot_id):
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner_id, project_id=project_id),
    ) as session:
        session.add(BotInstance(
            project_id=project_id, telegram_bot_id=bot_id,
            username="deletion_" + str(bot_id),
            display_name="Deletion regression bot",
            token_ciphertext="encrypted-test-token",
        ))


async def test_folder_product_and_draft_asset_deletion_keeps_sort_order_and_rls():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other_project, _, _, _ = (
            await setup_subscriber(factory)
        )
        bot, other_bot = tg_id(), tg_id()
        await _register_bot(factory, project_id=project, owner_id=owner, bot_id=bot)
        await _register_bot(factory, project_id=project, owner_id=owner, bot_id=other_bot)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            method = PaymentMethod(
                project_id=project, name="Manual RUB",
                provider="manual_settlement",
                instruction_template="Transfer {amount} {currency} for {plan}",
                status="active",
            )
            session.add(method)
            await session.flush()
            session.add(PaymentMethodCurrency(
                payment_method_id=method.id,
                project_id=project, currency_code="RUB",
            ))
        folder = await svc.add_folder(
            factory, project_id=project, bot_id=bot, actor_id=owner,
            name="My folder",
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.delete_empty_folder(
                factory, project_id=project, bot_id=bot,
                actor_id=outsider, folder_id=folder.id,
            )
        with pytest.raises((PermissionError, TenantAccessDenied, ValueError)):
            await svc.delete_empty_folder(
                factory, project_id=other_project, bot_id=bot,
                actor_id=owner, folder_id=folder.id,
            )

        product = await svc.create_product(
            factory, project_id=project, bot_id=bot, actor_id=owner,
            folder_id=folder.id, name="Unsold package", stars=None,
            manual_currency="RUB", manual_amount="100",
        )
        for key in ("first", "second", "third"):
            await svc.add_product_asset(
                factory, project_id=project, bot_id=bot, actor_id=owner,
                product_id=product.id, kind="document", file_id=key,
            )
        # A folder with products can now be removed; defer the destructive
        # operation until after testing draft-attachment maintenance.
        item, assets = await svc.list_product_assets(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=product.id,
        )
        assert item.id == product.id
        assert [(a.sort_order, a.file_id) for a in assets] == [
            (0, "first"), (1, "second"), (2, "third"),
        ]
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.delete_draft_asset(
                factory, project_id=project, bot_id=bot,
                actor_id=outsider, product_id=product.id,
                asset_id=assets[1].id,
            )
        with pytest.raises(ValueError):
            await svc.delete_draft_asset(
                factory, project_id=project, bot_id=bot,
                actor_id=owner, product_id=product.id, asset_id=uuid4(),
            )
        assert await svc.delete_draft_asset(
            factory, project_id=project, bot_id=bot, actor_id=owner,
            product_id=product.id, asset_id=assets[1].id,
        ) == 2
        await svc.add_product_asset(
            factory, project_id=project, bot_id=bot, actor_id=owner,
            product_id=product.id, kind="document", file_id="fourth",
        )
        _, updated = await svc.list_product_assets(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=product.id,
        )
        assert [(a.sort_order, a.file_id) for a in updated] == [
            (0, "first"), (1, "third"), (2, "fourth"),
        ]
        with pytest.raises((PermissionError, TenantAccessDenied, ValueError)):
            await svc.delete_unsold_product(
                factory, project_id=project, bot_id=other_bot,
                actor_id=owner, product_id=product.id,
            )

        assert await svc.delete_unsold_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=product.id,
        ) == folder.id
        async with system_project_session_scope(factory, project) as session:
            assert await session.get(FileProduct, product.id) is None
            assert (await session.scalars(select(FileAsset).where(
                FileAsset.product_id == product.id,
            ))).all() == []
            assert (await session.scalars(select(FileManualPrice).where(
                FileManualPrice.product_id == product.id,
            ))).all() == []

        # Folders are project-wide; products from another bot also block deletion.
        other_product = await svc.create_product(
            factory, project_id=project, bot_id=other_bot,
            actor_id=owner, folder_id=folder.id,
            name="Other bot unsold item", stars=10,
        )
        with pytest.raises(ValueError):
            await svc.delete_empty_folder(
                factory, project_id=project, bot_id=bot,
                actor_id=owner, folder_id=folder.id,
            )
        await svc.delete_unsold_product(
            factory, project_id=project, bot_id=other_bot,
            actor_id=owner, product_id=other_product.id,
        )
        await svc.delete_empty_folder(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, folder_id=folder.id,
        )
        async with system_project_session_scope(factory, project) as session:
            assert await session.get(FileFolder, folder.id) is None
    finally:
        await engine.dispose()


async def test_unpaid_file_invoices_do_not_block_removal_but_paid_files_do():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        bot = tg_id()
        await _register_bot(factory, project_id=project, owner_id=owner, bot_id=bot)
        folder = await svc.add_folder(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, name="History",
        )
        product = await svc.create_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, folder_id=folder.id,
            name="Pending Stars invoice", stars=17,
        )
        await svc.add_product_asset(
            factory, project_id=project, bot_id=bot, actor_id=owner,
            product_id=product.id, kind="document", file_id="paid-file",
        )
        await svc.change_product_status(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=product.id, status="published",
        )
        invoice = await svc.create_stars_order(
            factory, project_id=project, bot_id=bot,
            buyer_id=buyer_tid, product_id=product.id,
        )
        assert await svc.delete_unsold_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=product.id,
        ) == folder.id
        async with system_project_session_scope(factory, project) as session:
            retained = await session.get(FileProduct, product.id)
            assert retained is not None and retained.removed_at is not None
            assert retained.status == "archived"
        assert not await svc.list_products(
            factory, project_id=project, bot_id=bot,
            folder_id=folder.id, actor_id=owner,
        )

        # A pending invoice may still settle after the catalog item vanishes;
        # media and receipts survive precisely for this reason.
        paid = await svc.confirm_stars_payment(
            factory, project_id=project, bot_id=bot, buyer_id=buyer_tid,
            invoice_payload=invoice.invoice_payload,
            amount=17, currency="XTR", charge_id="test_del_" + uuid4().hex,
        )
        order, retained_assets = await svc.purchased_assets(
            factory, project_id=project, bot_id=bot,
            buyer_id=buyer_tid, purchase_id=paid.id,
        )
        assert order.id == paid.id
        assert [a.file_id for a in retained_assets] == ["paid-file"]
        with pytest.raises(ValueError, match="покупка|purchased"):
            await svc.delete_empty_folder(
                factory, project_id=project, bot_id=bot,
                actor_id=owner, folder_id=folder.id,
            )

        # A rejected/cancelled MANUAL invoice remains immutable, but does not
        # prevent soft removal of a second product.
        second = await svc.create_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, folder_id=folder.id,
            name="Manually billed", stars=10,
        )
        async with system_project_session_scope(factory, project) as session:
            session.add(FileManualOrder(
                project_id=project, product_id=second.id,
                telegram_bot_id=bot, telegram_user_id=buyer_tid,
                product_name="Manually billed", amount=10,
                currency_code="RUB", payment_method_id=uuid4(),
                payment_method_name="Manual", instruction="Test",
                status="cancelled",
            ))
        await svc.delete_unsold_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, product_id=second.id,
        )
        async with system_project_session_scope(factory, project) as session:
            retained_second = await session.get(FileProduct, second.id)
            assert retained_second is not None
            assert retained_second.removed_at is not None
            rows = (await session.scalars(
                select(FileManualOrder).where(FileManualOrder.product_id == second.id)
            )).all()
            assert len(rows) == 1 and rows[0].status == "cancelled"
    finally:
        await engine.dispose()


async def test_folder_with_only_unpaid_product_is_removable_with_invoice_preserved():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, _, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        bot = tg_id()
        await _register_bot(factory, project_id=project, owner_id=owner, bot_id=bot)
        folder = await svc.add_folder(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, name="Only unpaid",
        )
        product = await svc.create_product(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, folder_id=folder.id,
            name="Pending item", stars=10,
        )
        async with system_project_session_scope(factory, project) as session:
            session.add(FileManualOrder(
                project_id=project, product_id=product.id,
                telegram_bot_id=bot, telegram_user_id=buyer_tid,
                product_name="Pending item", amount=10,
                currency_code="RUB", payment_method_id=uuid4(),
                payment_method_name="Manual", instruction="Test",
                status="pending",
            ))
        await svc.delete_empty_folder(
            factory, project_id=project, bot_id=bot,
            actor_id=owner, folder_id=folder.id,
        )
        assert all(f.id != folder.id for f in await svc.list_folders(
            factory, project_id=project, bot_id=bot, actor_id=owner,
        ))
        async with system_project_session_scope(factory, project) as session:
            old_folder = await session.get(FileFolder, folder.id)
            old_product = await session.get(FileProduct, product.id)
            assert old_folder is not None and old_folder.removed_at is not None
            assert old_product is not None and old_product.removed_at is not None
            preserved = (await session.scalars(
                select(FileManualOrder).where(FileManualOrder.product_id == product.id)
            )).all()
            assert len(preserved) == 1 and preserved[0].status == "pending"
    finally:
        await engine.dispose()
