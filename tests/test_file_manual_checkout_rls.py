"""End-to-end PostgreSQL guardrails for manual file purchases."""
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.file_sales import service as files
from telegram_membership_saas.modules.file_sales.manual import (
    create_manual_file_order,
    list_file_methods,
    list_manual_paid,
    mark_manual_receipt_forwarded,
    purchased_manual_assets,
    review_manual_file_order,
    set_file_price,
    submit_manual_receipt,
)
from telegram_membership_saas.modules.file_sales.models import FileManualPurchase
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import list_owner_methods
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


async def test_manual_file_order_requires_approval_and_never_creates_subscription():
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
                username="manual_file_bot", display_name="Manual test",
                token_ciphertext="test-token",
            ))
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Сбербанк", currency_code="RUB",
            instruction="Send {amount} {currency} for {plan}",
        )
        method_id = (await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        ))[0].id
        folder = await files.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Файлы",
        )
        item = await files.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id,
            name="Видео", stars=0,
        )
        with pytest.raises(ValueError):
            await files.create_stars_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, product_id=item.id,
            )
        with pytest.raises(ValueError):
            await files.change_product_status(
                factory, project_id=project, bot_id=bot_id,
                actor_id=owner, product_id=item.id, status="published",
            )
        with pytest.raises((PermissionError, ValueError)):
            await set_file_price(
                factory, project_id=project, bot_id=bot_id,
                actor_id=outsider, product_id=item.id,
                currency="RUB", value="1",
            )
        await set_file_price(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=item.id,
            currency="RUB", value="525",
        )
        await files.add_product_asset(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=item.id,
            kind="document", file_id="video-file",
        )
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=item.id, status="published",
        )
        methods = await list_file_methods(
            factory, project_id=project, bot_id=bot_id, product_id=item.id,
        )
        assert any(
            m.currency == "RUB" and m.amount == Decimal("525")
            and m.method_id == method_id for m in methods
        )
        order = await create_manual_file_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, product_id=item.id,
            currency="RUB", method_id=method_id,
        )
        assert order.status == "waiting_confirmation"
        assert order.amount == Decimal("525")
        assert "525 RUB" in order.instruction
        with pytest.raises(PermissionError):
            await purchased_manual_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_tid, order_id=order.id,
            )
        submitted, reviewers = await submit_manual_receipt(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, order_id=order.id,
        )
        assert submitted.status == "waiting_review"
        assert reviewers
        async with system_project_session_scope(factory, project) as session:
            owner_tid = await session.scalar(
                select(TelegramUser.telegram_user_id).where(TelegramUser.id == owner)
            )
            outside_tid = await session.scalar(
                select(TelegramUser.telegram_user_id).where(TelegramUser.id == outsider)
            )
        with pytest.raises((PermissionError, ValueError)):
            await review_manual_file_order(
                factory, project_id=project, bot_id=bot_id,
                reviewer_id=owner_tid, reviewer_name="owner",
                reviewer_first_name="Owner", order_id=order.id, approve=True,
            )
        # The runtime DB role cannot forge a paid state without proof.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(
                    text(
                        "UPDATE file_sale_manual_purchases "
                        "SET status='paid', reviewed_by=:owner, paid_at=now() "
                        "WHERE id=:id"
                    ), {"owner": owner, "id": order.id},
                )
        await mark_manual_receipt_forwarded(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, order_id=order.id,
        )
        with pytest.raises((PermissionError, ValueError)):
            await review_manual_file_order(
                factory, project_id=project, bot_id=bot_id,
                reviewer_id=outside_tid, reviewer_name="stranger",
                reviewer_first_name="Stranger", order_id=order.id, approve=True,
            )
        approved, changed = await review_manual_file_order(
            factory, project_id=project, bot_id=bot_id,
            reviewer_id=owner_tid, reviewer_name="owner",
            reviewer_first_name="Owner", order_id=order.id, approve=True,
        )
        assert changed and approved.status == "paid"
        _, again = await review_manual_file_order(
            factory, project_id=project, bot_id=bot_id,
            reviewer_id=owner_tid, reviewer_name="owner",
            reviewer_first_name="Owner", order_id=order.id, approve=True,
        )
        assert not again
        with pytest.raises(PermissionError):
            await purchased_manual_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=tg_id(), order_id=order.id,
            )
        paid, assets = await purchased_manual_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, order_id=order.id,
        )
        assert paid.id == order.id and assets[0].file_id == "video-file"
        await files.change_product_status(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, product_id=item.id, status="archived",
        )
        assert (await purchased_manual_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_tid, order_id=order.id,
        ))[1][0].file_id == "video-file"
        count, history = await list_manual_paid(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer_tid,
        )
        assert count == 1 and history[0].id == order.id
        # Even a forged INSERT with all fields set cannot create paid access.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(
                    text(
                        "INSERT INTO file_sale_manual_purchases "
                        "(id, project_id, product_id, telegram_bot_id, "
                        "telegram_user_id, product_name, amount, currency_code, "
                        "payment_method_id, payment_method_name, instruction, status, "
                        "submitted_at, receipt_forwarded_at, paid_at, reviewed_by) "
                        "SELECT :newid, project_id, product_id, telegram_bot_id, "
                        "telegram_user_id, product_name, amount, currency_code, "
                        "payment_method_id, payment_method_name, instruction, "
                        "'paid', now(), now(), now(), :owner "
                        "FROM file_sale_manual_purchases WHERE id=:id"
                    ), {"newid": uuid4(), "owner": owner, "id": order.id},
                )
        for statement in (
            "UPDATE file_sale_manual_purchases SET amount=1 WHERE id=:id",
            "DELETE FROM file_sale_manual_purchases WHERE id=:id",
        ):
            with pytest.raises(DBAPIError):
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    await session.execute(text(statement), {"id": order.id})
        async with system_project_session_scope(factory, project) as session:
            assert not await session.scalar(select(func.count(Subscription.id)).where(
                Subscription.project_id == project,
            ))
            snapshot = await session.scalar(select(FileManualPurchase).where(
                FileManualPurchase.id == order.id,
            ))
            assert snapshot.amount == Decimal("525")
            assert snapshot.status == "paid"
    finally:
        await engine.dispose()
