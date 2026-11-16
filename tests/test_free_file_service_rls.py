"""Free file delivery is a tenant/buyer-scoped catalog read, not a payment."""
import os
from uuid import uuid4

import pytest
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales import manual_service as manual
from telegram_membership_saas.modules.file_sales import service as files
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


async def test_free_file_price_zero_publishes_and_delivers_without_payment():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="free_file_test", display_name="Free downloads",
                token_ciphertext="encrypted-test-token",
            ))
        folder = await files.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Samples",
        )
        # Owner can publish a genuinely free file from the Stars field.
        free = await files.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id, name="Free preview", stars=0,
        )
        await files.add_product_asset(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            product_id=free.id, kind="document", file_id="original-file-id",
        )
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=free.id, status="published",
        )
        product, assets = await files.free_product_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=free.id,
        )
        assert product.is_free is True
        assert product.stars_price is None
        assert [asset.file_id for asset in assets] == ["original-file-id"]

        # Old paid callbacks cannot create invoices or approvals for free files.
        with pytest.raises(ValueError, match="free|бесплат"):
            await files.create_stars_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, product_id=free.id,
            )
        with pytest.raises(ValueError, match="free|бесплат"):
            await manual.create_manual_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, product_id=free.id,
                currency="RUB", method_id=uuid4(),
            )
        with pytest.raises((ValueError, PermissionError)):
            await files.free_product_assets(
                factory, project_id=project, bot_id=tg_id(),
                buyer_id=buyer_tid, product_id=free.id,
            )

        # Existing paid-product currency editor accepts 0 without needing
        # an active payment method, and does not create a zero payment row.
        paid = await files.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id, name="Switch to free", stars=99,
        )
        await files.add_product_asset(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            product_id=paid.id, kind="document", file_id="original-paid-asset",
        )
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=paid.id, status="published",
        )
        await manual.set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=paid.id,
            currency="RUB", value="0",
        )
        product, _ = await files.free_product_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=paid.id,
        )
        assert product.is_free is True

        with pytest.raises((ValueError, PermissionError)):
            await manual.set_manual_price(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, product_id=paid.id,
                currency="RUB", value="0",
            )

        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=paid.id, status="archived",
        )
        with pytest.raises(PermissionError):
            await files.free_product_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, product_id=paid.id,
            )
    finally:
        await engine.dispose()
