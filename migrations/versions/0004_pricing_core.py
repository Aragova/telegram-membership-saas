"""add currency registry and versioned plan prices

Revision ID: 0004_pricing_core
Revises: 0003_resources_plans
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_pricing_core"
down_revision: str | None = "0003_resources_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"

DEFAULT_CURRENCIES = [
    ("RUB", "fiat", "Russian Ruble", "₽", 2),
    ("USD", "fiat", "US Dollar", "$", 2),
    ("EUR", "fiat", "Euro", "€", 2),
    ("GBP", "fiat", "British Pound", "£", 2),
    ("KZT", "fiat", "Kazakhstani Tenge", "₸", 2),
    ("BYN", "fiat", "Belarusian Ruble", "Br", 2),
    ("UAH", "fiat", "Ukrainian Hryvnia", "₴", 2),
    ("GEL", "fiat", "Georgian Lari", "₾", 2),
    ("AMD", "fiat", "Armenian Dram", "֏", 2),
    ("AED", "fiat", "UAE Dirham", "د.إ", 2),
    ("BTC", "crypto", "Bitcoin", "₿", 8),
    ("ETH", "crypto", "Ethereum", "Ξ", 18),
    ("USDT", "crypto", "Tether", "₮", 6),
    ("USDC", "crypto", "USD Coin", "USDC", 6),
    ("TON", "crypto", "Toncoin", "TON", 9),
    ("TRX", "crypto", "TRON", "TRX", 6),
]


def upgrade() -> None:
    op.create_table(
        "currency_assets",
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("asset_type", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("precision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("asset_type IN ('fiat', 'crypto')", name="ck_currency_assets_type"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_currency_assets_status"),
        sa.CheckConstraint(
            "precision >= 0 AND precision <= 18",
            name="ck_currency_assets_precision",
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    currency_assets = sa.table(
        "currency_assets",
        sa.column("code", sa.String()),
        sa.column("asset_type", sa.String()),
        sa.column("name", sa.String()),
        sa.column("symbol", sa.String()),
        sa.column("precision", sa.Integer()),
        sa.column("status", sa.String()),
    )
    op.bulk_insert(
        currency_assets,
        [
            {
                "code": code,
                "asset_type": asset_type,
                "name": name,
                "symbol": symbol,
                "precision": precision,
                "status": "active",
            }
            for code, asset_type, name, symbol, precision in DEFAULT_CURRENCIES
        ],
    )

    op.create_table(
        "plan_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("currency_code", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "active_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_plan_prices_amount_positive"),
        sa.CheckConstraint("version >= 1", name="ck_plan_prices_version_positive"),
        sa.CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_plan_prices_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND active_until IS NULL) OR "
            "(status = 'retired' AND active_until IS NOT NULL)",
            name="ck_plan_prices_active_window",
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currency_assets.code"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_prices_plan_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_plan_prices_id_project"),
        sa.UniqueConstraint(
            "plan_id",
            "currency_code",
            "version",
            name="uq_plan_prices_plan_currency_version",
        ),
    )
    op.create_index("ix_plan_prices_project_id", "plan_prices", ["project_id"])
    op.create_index("ix_plan_prices_plan_id", "plan_prices", ["plan_id"])
    op.create_index(
        "uq_plan_prices_active_pair",
        "plan_prices",
        ["plan_id", "currency_code"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.execute("ALTER TABLE plan_prices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_prices FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY plan_prices_tenant_policy ON plan_prices
        USING (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        WITH CHECK (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS plan_prices_tenant_policy ON plan_prices")
    op.execute("ALTER TABLE plan_prices NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_prices DISABLE ROW LEVEL SECURITY")
    op.drop_index("uq_plan_prices_active_pair", table_name="plan_prices")
    op.drop_index("ix_plan_prices_plan_id", table_name="plan_prices")
    op.drop_index("ix_plan_prices_project_id", table_name="plan_prices")
    op.drop_table("plan_prices")
    op.drop_table("currency_assets")
