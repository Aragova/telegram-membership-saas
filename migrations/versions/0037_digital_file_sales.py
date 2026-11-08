"""Tenant file catalog and payment-confirmed digital delivery journal.

Revision ID: 0037_digital_file_sales
Revises: 0036_bot_ownership_transfer
"""
# ruff: noqa: E501
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_digital_file_sales"
down_revision: str | None = "0036_bot_ownership_transfer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "file_sale_folders", "file_sale_products", "file_sale_assets",
    "file_sale_purchases",
)


def upgrade() -> None:
    op.create_table(
        "file_sale_folders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("id", "project_id", name="uq_file_folder_project"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_file_folder_status"),
    )
    op.create_table(
        "file_sale_products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("stars_price", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("id", "project_id", name="uq_file_product_project"),
        sa.ForeignKeyConstraint(["folder_id", "project_id"], ["file_sale_folders.id", "file_sale_folders.project_id"], name="fk_file_product_folder_project"),
        sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_file_product_status"),
        sa.CheckConstraint("stars_price BETWEEN 1 AND 250000", name="ck_file_product_price"),
    )
    op.create_index("ix_file_sale_catalog", "file_sale_products", ["project_id","status","folder_id"])
    op.create_table(
        "file_sale_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("file_id", sa.String(512), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["product_id", "project_id"], ["file_sale_products.id", "file_sale_products.project_id"], name="fk_file_asset_product_project"),
        sa.UniqueConstraint("product_id", "sort_order", name="uq_file_asset_order"),
        sa.CheckConstraint("kind IN ('document','photo','video','audio','animation')", name="ck_file_asset_kind"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 19", name="ck_file_asset_position"),
    )
    op.create_table(
        "file_sale_purchases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("product_name", sa.String(120), nullable=False),
        sa.Column("invoice_payload", sa.String(128), nullable=False),
        sa.Column("telegram_charge_id", sa.String(256)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["product_id", "project_id"], ["file_sale_products.id", "file_sale_products.project_id"], ondelete="CASCADE", name="fk_file_purchase_product_project"),
        sa.UniqueConstraint("invoice_payload", name="uq_file_purchase_payload"),
        sa.UniqueConstraint("telegram_charge_id", name="uq_file_purchase_charge"),
        sa.CheckConstraint("status IN ('pending','paid')", name="ck_file_purchase_status"),
        sa.CheckConstraint("stars_amount BETWEEN 1 AND 250000", name="ck_file_purchase_stars"),
    )
    op.create_index("ix_file_purchase_buyer", "file_sale_purchases", ["project_id","telegram_user_id","status"])

    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_scope ON public.{table}
            FOR ALL USING (
                project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid
            )
            WITH CHECK (
                project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid
            )
        """)
    op.execute("""
        CREATE FUNCTION public.guard_file_sale_purchase()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'paid' THEN
                    RAISE EXCEPTION 'Paid digital purchase history is immutable' USING ERRCODE = '42501';
                END IF;
                RETURN OLD;
            END IF;
            IF (
                NEW.id, NEW.project_id, NEW.product_id, NEW.telegram_bot_id,
                NEW.telegram_user_id, NEW.stars_amount, NEW.product_name,
                NEW.invoice_payload, NEW.created_at
            ) IS DISTINCT FROM (
                OLD.id, OLD.project_id, OLD.product_id, OLD.telegram_bot_id,
                OLD.telegram_user_id, OLD.stars_amount, OLD.product_name,
                OLD.invoice_payload, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'Digital purchase snapshot is immutable' USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'paid' AND (
                NEW.status IS DISTINCT FROM OLD.status
                OR NEW.telegram_charge_id IS DISTINCT FROM OLD.telegram_charge_id
                OR NEW.paid_at IS DISTINCT FROM OLD.paid_at
            ) THEN
                RAISE EXCEPTION 'Confirmed Stars payment cannot change' USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'pending' AND (
                NEW.status != 'paid'
                OR NEW.telegram_charge_id IS NULL
                OR NEW.paid_at IS NULL
                OR OLD.telegram_charge_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Invalid digital payment confirmation' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_file_sale_purchase
        BEFORE UPDATE OR DELETE ON file_sale_purchases
        FOR EACH ROW EXECUTE FUNCTION public.guard_file_sale_purchase()
    """)


def downgrade() -> None:
    # A live Stars charge must never be erased as part of a schema rollback.
    op.execute("""
        DO $check$
        BEGIN
            IF EXISTS (SELECT 1 FROM file_sale_purchases WHERE status = 'paid') THEN
                RAISE EXCEPTION 'Cannot drop paid digital file history';
            END IF;
        END $check$
    """)
    op.execute("DROP TRIGGER trg_guard_file_sale_purchase ON file_sale_purchases")
    op.execute("DROP FUNCTION public.guard_file_sale_purchase()")
    for table in reversed(TABLES):
        op.drop_table(table)
