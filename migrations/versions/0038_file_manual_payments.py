"""Explicit manual file prices and immutable purchase confirmation journal.

Revision ID: 0038_file_manual_payments
Revises: 0037_digital_file_sales
"""
# ruff: noqa: E501
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_file_manual_payments"
down_revision: str | None = "0037_digital_file_sales"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stars price 0 means Stars disabled; legacy positive XTR prices unchanged.
    op.drop_constraint("ck_file_product_price", "file_sale_products", type_="check")
    op.create_check_constraint(
        "ck_file_product_price", "file_sale_products",
        "stars_price BETWEEN 0 AND 250000",
    )
    op.create_table(
        "file_sale_prices",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("product_id", sa.Uuid(), primary_key=True),
        sa.Column("currency_code", sa.String(16), sa.ForeignKey(
            "currency_assets.code", ondelete="RESTRICT",
        ), primary_key=True),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_price_product_project",
        ),
        sa.CheckConstraint(
            "amount > 0 AND amount <= 1000000000", name="ck_file_price_amount",
        ),
    )
    op.create_table(
        "file_sale_manual_purchases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("product_name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency_code", sa.String(16), nullable=False),
        sa.Column("payment_method_id", sa.Uuid(), nullable=False),
        sa.Column("payment_method_name", sa.String(120), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("receipt_forwarded_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT")),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_product_project", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "amount > 0 AND amount <= 1000000000", name="ck_file_manual_amount",
        ),
        sa.CheckConstraint(
            "status IN ('waiting_confirmation','waiting_review','paid','rejected','cancelled')",
            name="ck_file_manual_status",
        ),
        sa.CheckConstraint(
            "((status = 'waiting_confirmation' AND submitted_at IS NULL AND receipt_forwarded_at IS NULL AND paid_at IS NULL AND reviewed_by IS NULL) OR (status = 'waiting_review' AND submitted_at IS NOT NULL AND paid_at IS NULL AND reviewed_by IS NULL) OR (status = 'paid' AND submitted_at IS NOT NULL AND receipt_forwarded_at IS NOT NULL AND paid_at IS NOT NULL AND reviewed_by IS NOT NULL) OR (status = 'rejected' AND submitted_at IS NOT NULL AND receipt_forwarded_at IS NOT NULL AND paid_at IS NULL AND reviewed_by IS NOT NULL) OR (status = 'cancelled' AND submitted_at IS NULL AND receipt_forwarded_at IS NULL AND paid_at IS NULL AND reviewed_by IS NULL))",
            name="ck_file_manual_state_timestamps",
        ),
    )
    op.create_index(
        "ix_file_manual_buyer", "file_sale_manual_purchases",
        ["project_id", "telegram_bot_id", "telegram_user_id", "status"],
    )
    for table in ("file_sale_prices", "file_sale_manual_purchases"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_scope ON public.{table} FOR ALL
            USING (
                project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid
            )
            WITH CHECK (
                project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid
            )
        """)
    # The only mutable status transitions are submission and a single review.
    # A paid file record can neither be rewritten nor erased.
    op.execute("""
        CREATE FUNCTION public.guard_file_manual_purchase()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status != 'waiting_confirmation'
                   OR NEW.submitted_at IS NOT NULL
                   OR NEW.receipt_forwarded_at IS NOT NULL
                   OR NEW.paid_at IS NOT NULL
                   OR NEW.reviewed_by IS NOT NULL THEN
                    RAISE EXCEPTION 'Manual orders must start unconfirmed'
                        USING ERRCODE = '42501';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'paid' THEN
                    RAISE EXCEPTION 'Paid manual file history is immutable'
                        USING ERRCODE = '42501';
                END IF;
                RETURN OLD;
            END IF;
            IF (
                NEW.id, NEW.project_id, NEW.product_id, NEW.telegram_bot_id,
                NEW.telegram_user_id, NEW.product_name, NEW.amount,
                NEW.currency_code, NEW.payment_method_id,
                NEW.payment_method_name, NEW.instruction, NEW.created_at
            ) IS DISTINCT FROM (
                OLD.id, OLD.project_id, OLD.product_id, OLD.telegram_bot_id,
                OLD.telegram_user_id, OLD.product_name, OLD.amount,
                OLD.currency_code, OLD.payment_method_id,
                OLD.payment_method_name, OLD.instruction, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'Manual file checkout snapshot is immutable'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'paid' THEN
                IF (
                    NEW.status, NEW.submitted_at, NEW.receipt_forwarded_at,
                    NEW.paid_at, NEW.reviewed_by
                ) IS DISTINCT FROM (
                    OLD.status, OLD.submitted_at, OLD.receipt_forwarded_at,
                    OLD.paid_at, OLD.reviewed_by
                ) THEN
                    RAISE EXCEPTION 'Confirmed file payment cannot change'
                        USING ERRCODE = '42501';
                END IF;
            ELSIF OLD.status = 'waiting_confirmation' THEN
                IF NEW.status NOT IN ('waiting_confirmation','waiting_review','cancelled') THEN
                    RAISE EXCEPTION 'Invalid manual file submission'
                        USING ERRCODE = '42501';
                END IF;
            ELSIF OLD.status = 'waiting_review' THEN
                IF NEW.status NOT IN ('waiting_review','paid','rejected')
                    OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
                    OR (
                        NEW.status IN ('paid', 'rejected')
                        AND (
                            OLD.receipt_forwarded_at IS NULL
                            OR NEW.receipt_forwarded_at IS DISTINCT FROM OLD.receipt_forwarded_at
                            OR NEW.reviewed_by IS NULL
                        )
                    ) THEN
                    RAISE EXCEPTION 'Invalid manual file review or missing proof'
                        USING ERRCODE = '42501';
                END IF;
            ELSE
                IF NEW.status IS DISTINCT FROM OLD.status THEN
                    RAISE EXCEPTION 'Manual file decision is final'
                        USING ERRCODE = '42501';
                END IF;
            END IF;
            RETURN NEW;
        END $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_file_manual_purchase
        BEFORE INSERT OR UPDATE OR DELETE ON public.file_sale_manual_purchases
        FOR EACH ROW EXECUTE FUNCTION public.guard_file_manual_purchase()
    """)


def downgrade() -> None:
    op.execute("""
        DO $check$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM file_sale_manual_purchases WHERE status = 'paid'
            ) THEN
                RAISE EXCEPTION 'Cannot drop paid manual file history';
            END IF;
        END $check$
    """)
    op.execute(
        "DROP TRIGGER trg_guard_file_manual_purchase "
        "ON public.file_sale_manual_purchases"
    )
    op.execute("DROP FUNCTION public.guard_file_manual_purchase()")
    op.drop_index("ix_file_manual_buyer", table_name="file_sale_manual_purchases")
    op.drop_table("file_sale_manual_purchases")
    op.drop_table("file_sale_prices")
    op.drop_constraint("ck_file_product_price", "file_sale_products", type_="check")
    op.create_check_constraint(
        "ck_file_product_price", "file_sale_products",
        "stars_price BETWEEN 1 AND 250000",
    )
