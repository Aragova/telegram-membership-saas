"""Native Stars subscription checkout and shared durable grant identity.

Revision ID: 0057_subscription_stars
Revises: 0056_saas_star_refunds
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_subscription_stars"
down_revision: str | None = "0056_saas_star_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO public.currency_assets(
            code,asset_type,name,symbol,precision,status
        ) VALUES ('XTR','fiat','Telegram Stars','⭐',0,'active')
        ON CONFLICT(code) DO NOTHING
    """)
    op.create_table(
        "subscription_star_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_price_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("invoice_payload", sa.String(128), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("plan_name_snapshot", sa.String(120), nullable=False),
        sa.Column("duration_value", sa.Integer(), nullable=True),
        sa.Column("duration_unit", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("telegram_charge_id", sa.String(256), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id","project_id"],
            ["project_customers.id","project_customers.project_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id","project_id"], ["plans.id","plans.project_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_price_id","project_id","plan_id"],
            ["plan_prices.id","plan_prices.project_id","plan_prices.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id","project_id",
                            name="uq_subscription_star_order_project"),
        sa.UniqueConstraint("invoice_payload",
                            name="uq_subscription_star_payload"),
        sa.UniqueConstraint("telegram_bot_id","telegram_charge_id",
                            name="uq_subscription_star_bot_charge"),
        sa.CheckConstraint("stars_amount BETWEEN 1 AND 250000",
                           name="ck_subscription_star_amount"),
        sa.CheckConstraint(
            "(status='pending' AND telegram_charge_id IS NULL AND paid_at IS NULL)"
            " OR (status='paid' AND telegram_charge_id IS NOT NULL "
            "AND paid_at IS NOT NULL)",
            name="ck_subscription_star_state",
        ),
        sa.CheckConstraint(
            "(duration_unit='lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day','week','month','year') "
            "AND duration_value IS NOT NULL AND duration_value>0)",
            name="ck_subscription_star_duration",
        ),
    )
    op.create_index(
        "ix_subscription_star_order_customer",
        "subscription_star_orders", ["project_id","customer_id","created_at"],
    )
    op.execute("""
        ALTER TABLE public.subscription_star_orders ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.subscription_star_orders FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE POLICY subscription_star_orders_scope
        ON public.subscription_star_orders
        FOR ALL USING(
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        ) WITH CHECK(
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
    """)
    op.alter_column("subscriptions","last_payment_id",
                    existing_type=sa.Uuid(),nullable=True)
    op.add_column(
        "subscriptions",
        sa.Column("last_star_order_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_last_star_order_project",
        "subscriptions","subscription_star_orders",
        ["last_star_order_id","project_id"],["id","project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_subscriptions_last_paid_source","subscriptions",
        "(last_payment_id IS NOT NULL) OR (last_star_order_id IS NOT NULL)",
    )
    op.drop_constraint("ck_entitlement_grants_source",
                       "entitlement_grants",type_="check")
    op.create_check_constraint(
        "ck_entitlement_grants_source","entitlement_grants",
        "source IN ('payment','stars_payment','manual','owner_service',"
        "'promocode','trial','referral','import','compensation')",
    )
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT SELECT,INSERT,UPDATE ON public.subscription_star_orders
            TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Refuse to remove immutable Stars subscription sales and paid grants."
    )
