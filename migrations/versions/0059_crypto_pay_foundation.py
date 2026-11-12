"""Encrypted merchant Crypto Pay app credentials + frozen invoice evidence.

Revision ID: 0059_crypto_pay_foundation
Revises: 0058_stars_owner_history_scope
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_crypto_pay_foundation"
down_revision: str | None = "0058_stars_owner_history_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crypto_pay_apps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("app_label", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["telegram_users.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "project_id", name="uq_crypto_pay_app_project"),
        sa.CheckConstraint("environment IN ('testnet','mainnet')",
                           name="ck_crypto_pay_app_environment"),
        sa.CheckConstraint("status IN ('active','retired')",
                           name="ck_crypto_pay_app_status"),
    )
    op.create_index(
        "uq_crypto_pay_active_bot", "crypto_pay_apps",
        ["project_id", "telegram_bot_id"], unique=True,
        postgresql_where=sa.text("status='active'"),
    )
    op.create_table(
        "crypto_pay_invoices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_id", sa.Uuid()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("plan_id", sa.Uuid()),
        sa.Column("plan_price_id", sa.Uuid()),
        sa.Column("product_id", sa.Uuid()),
        sa.Column("name_snapshot", sa.String(120), nullable=False),
        sa.Column("duration_value", sa.Integer()),
        sa.Column("duration_unit", sa.String(16)),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("currency_code", sa.String(16), nullable=False),
        sa.Column("currency_type", sa.String(16), nullable=False),
        sa.Column("payload", sa.String(128), nullable=False),
        sa.Column("provider_invoice_id", sa.BigInteger()),
        sa.Column("invoice_url", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["app_id", "project_id"],
            ["crypto_pay_apps.id", "crypto_pay_apps.project_id"],
            name="fk_crypto_pay_invoice_app_project", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "project_id", name="uq_crypto_pay_invoice_project"),
        sa.UniqueConstraint("payload", name="uq_crypto_pay_invoice_payload"),
        sa.UniqueConstraint(
            "app_id", "provider_invoice_id", name="uq_crypto_pay_app_invoice",
        ),
        sa.CheckConstraint(
            "(kind='subscription' AND plan_id IS NOT NULL "
            "AND plan_price_id IS NOT NULL AND product_id IS NULL "
            "AND customer_id IS NOT NULL AND duration_unit IS NOT NULL) "
            "OR (kind='file' AND product_id IS NOT NULL AND plan_id IS NULL "
            "AND plan_price_id IS NULL AND customer_id IS NULL "
            "AND duration_unit IS NULL)",
            name="ck_crypto_pay_invoice_target",
        ),
        sa.CheckConstraint("amount>0", name="ck_crypto_pay_invoice_amount"),
        sa.CheckConstraint(
            "currency_type IN ('fiat','crypto')",
            name="ck_crypto_pay_invoice_currency_type",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','issued','paid','expired','cancelled')",
            name="ck_crypto_pay_invoice_status",
        ),
        sa.CheckConstraint(
            "(status='reserved' AND provider_invoice_id IS NULL AND paid_at IS NULL) "
            "OR (status='issued' AND provider_invoice_id IS NOT NULL "
            "AND paid_at IS NULL) "
            "OR (status='paid' AND provider_invoice_id IS NOT NULL "
            "AND paid_at IS NOT NULL) "
            "OR (status IN ('expired','cancelled') AND paid_at IS NULL)",
            name="ck_crypto_pay_invoice_state",
        ),
    )
    op.create_index(
        "ix_crypto_pay_invoice_pending", "crypto_pay_invoices",
        ["app_id", "status", "created_at"],
    )
    op.create_index(
        "ix_crypto_pay_invoice_buyer", "crypto_pay_invoices",
        ["project_id", "telegram_bot_id", "telegram_user_id", "created_at"],
    )
    op.execute("ALTER TABLE crypto_pay_apps ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE crypto_pay_apps FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY crypto_pay_apps_owner_or_worker
        ON crypto_pay_apps FOR ALL
        USING (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
            AND (
              NULLIF(current_setting('app.current_user_id',true),'') IS NULL
              OR owner_user_id =
                  NULLIF(current_setting('app.current_user_id',true),'')::uuid
            )
        )
        WITH CHECK (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
            AND (
              NULLIF(current_setting('app.current_user_id',true),'') IS NULL
              OR owner_user_id =
                  NULLIF(current_setting('app.current_user_id',true),'')::uuid
            )
        )
    """)
    op.execute("ALTER TABLE crypto_pay_invoices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE crypto_pay_invoices FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY crypto_pay_invoices_project ON crypto_pay_invoices
        FOR ALL
        USING (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
        WITH CHECK (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
    """)
    # Production runtime role already exists; CI creates it AFTER migrations.
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT SELECT,INSERT,UPDATE ON crypto_pay_apps TO membership_runtime;
            GRANT SELECT,INSERT,UPDATE ON crypto_pay_invoices TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    raise RuntimeError("Refuse to delete encrypted Crypto Pay credentials or invoices.")
