"""Manual payment prices and review journal for file sales.

Revision ID: 0038_file_manual_checkout
Revises: 0037_digital_file_sales
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_file_manual_checkout"
down_revision: str | None = "0037_digital_file_sales"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing products retain Stars prices; future manual-only files may use NULL.
    op.alter_column("file_sale_products", "stars_price",
                    existing_type=sa.Integer(), nullable=True)
    op.create_table(
        "file_sale_manual_prices",
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("currency_code", sa.String(16), sa.ForeignKey(
            "currency_assets.code", ondelete="RESTRICT",
        ), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_price_product", ondelete="CASCADE",
        ),
        sa.CheckConstraint("amount > 0", name="ck_file_manual_price_positive"),
    )
    op.create_index("ix_file_sale_manual_prices_project_id",
                    "file_sale_manual_prices", ["project_id"])
    op.create_table(
        "file_sale_manual_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("currency_code", sa.String(16), nullable=False),
        sa.Column("payment_method_id", sa.Uuid(), nullable=False),
        sa.Column("payment_method_name", sa.String(120), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("receipt_kind", sa.String(16), nullable=True),
        sa.Column("receipt_file_id", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_order_product", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("amount > 0", name="ck_file_manual_order_positive"),
        sa.CheckConstraint(
            "status IN ('pending','submitted','approved','rejected','cancelled')",
            name="ck_file_manual_order_status",
        ),
    )
    op.create_index("ix_file_manual_orders_buyer", "file_sale_manual_orders",
                    ["project_id", "telegram_user_id", "status"])
    op.create_index("ix_file_manual_orders_reviews", "file_sale_manual_orders",
                    ["project_id", "status", "created_at"])

    for table in ("file_sale_manual_prices", "file_sale_manual_orders"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_project_scope ON public.{table} FOR ALL
            USING (project_id = NULLIF(
                current_setting('app.current_project_id', true), ''
            )::uuid)
            WITH CHECK (project_id = NULLIF(
                current_setting('app.current_project_id', true), ''
            )::uuid)
        """)

    # Keep every submitted/approved/rejected evidence trail; retries are idempotent.
    op.execute("""
        CREATE FUNCTION public.guard_file_manual_order()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'pending' AND OLD.status <> 'cancelled' THEN
                    RAISE EXCEPTION 'Reviewed file payments are immutable'
                        USING ERRCODE = '42501';
                END IF;
                RETURN OLD;
            END IF;
            IF (NEW.id, NEW.project_id, NEW.product_id, NEW.telegram_bot_id,
                NEW.telegram_user_id, NEW.product_name, NEW.amount,
                NEW.currency_code, NEW.payment_method_id, NEW.payment_method_name,
                NEW.instruction, NEW.created_at)
                IS DISTINCT FROM
               (OLD.id, OLD.project_id, OLD.product_id, OLD.telegram_bot_id,
                OLD.telegram_user_id, OLD.product_name, OLD.amount,
                OLD.currency_code, OLD.payment_method_id, OLD.payment_method_name,
                OLD.instruction, OLD.created_at) THEN
                RAISE EXCEPTION 'Manual file checkout snapshot is immutable'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status IN ('approved','rejected','cancelled') AND
                (NEW.status, NEW.paid_at, NEW.reviewed_at, NEW.reviewed_by,
                 NEW.receipt_file_id, NEW.receipt_kind, NEW.submitted_at)
                IS DISTINCT FROM
                (OLD.status, OLD.paid_at, OLD.reviewed_at, OLD.reviewed_by,
                 OLD.receipt_file_id, OLD.receipt_kind, OLD.submitted_at) THEN
                RAISE EXCEPTION 'Final file payment decision is immutable'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'pending' AND NEW.status NOT IN
                ('pending','submitted','cancelled') THEN
                RAISE EXCEPTION 'Invalid manual file payment transition'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'submitted' AND NEW.status NOT IN
                ('submitted','approved','rejected') THEN
                RAISE EXCEPTION 'Invalid manual file review transition'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'submitted' AND
                (NEW.receipt_file_id, NEW.receipt_kind, NEW.submitted_at)
                IS DISTINCT FROM
                (OLD.receipt_file_id, OLD.receipt_kind, OLD.submitted_at) THEN
                RAISE EXCEPTION 'Submitted file receipt is immutable'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_file_manual_order
        BEFORE UPDATE OR DELETE ON file_sale_manual_orders
        FOR EACH ROW EXECUTE FUNCTION public.guard_file_manual_order()
    """)

    # The existing transfer function (0036) checks subscription liabilities.
    # This independent trigger also protects pending manual file obligations,
    # including the privileged SQL ownership-transfer path.
    op.execute("""
        CREATE FUNCTION public.guard_project_file_liabilities()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $file_guard$
        BEGIN
            IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
               AND EXISTS (
                   SELECT 1 FROM public.file_sale_manual_orders f
                   WHERE f.project_id = OLD.id
                     AND f.status IN ('pending', 'submitted')
               )
            THEN
                RAISE EXCEPTION
                    'Open manual file orders block ownership transfer'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END $file_guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_project_file_liabilities
        BEFORE UPDATE OF owner_user_id ON public.projects
        FOR EACH ROW EXECUTE FUNCTION public.guard_project_file_liabilities()
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_project_file_liabilities() FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_guard_project_file_liabilities ON public.projects")
    op.execute("DROP FUNCTION public.guard_project_file_liabilities()")
    op.execute("""
        DO $check$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM file_sale_manual_orders
                WHERE status IN ('submitted','approved','rejected')
            ) THEN
                RAISE EXCEPTION 'Cannot remove financial file payment evidence';
            END IF;
        END $check$
    """)
    op.execute("DROP TRIGGER trg_guard_file_manual_order ON file_sale_manual_orders")
    op.execute("DROP FUNCTION public.guard_file_manual_order()")
    op.drop_table("file_sale_manual_orders")
    op.drop_table("file_sale_manual_prices")
    op.execute("""
        DO $check$
        BEGIN
            IF EXISTS (SELECT 1 FROM file_sale_products WHERE stars_price IS NULL) THEN
                RAISE EXCEPTION 'Cannot require Stars prices for manual-only files';
            END IF;
        END $check$
    """)
    op.alter_column("file_sale_products", "stars_price",
                    existing_type=sa.Integer(), nullable=False)
