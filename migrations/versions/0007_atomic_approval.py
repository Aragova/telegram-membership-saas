"""add atomic payment approval domain records

Revision ID: 0007_atomic_approval
Revises: 0006_payment_requests
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_atomic_approval"
down_revision: str | None = "0006_payment_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def _enable_project_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table_name}_tenant_policy ON {table_name}
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


def upgrade() -> None:
    op.add_column(
        "payment_requests",
        sa.Column("duration_value", sa.Integer(), nullable=True),
    )
    op.add_column(
        "payment_requests",
        sa.Column("duration_unit", sa.String(length=16), nullable=True),
    )
    op.execute(
        """
        UPDATE payment_requests AS pr
        SET duration_value = p.duration_value,
            duration_unit = p.duration_unit
        FROM plans AS p
        WHERE p.id = pr.plan_id
          AND p.project_id = pr.project_id
        """
    )
    op.alter_column(
        "payment_requests",
        "duration_unit",
        existing_type=sa.String(length=16),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_payment_requests_duration",
        "payment_requests",
        "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
        "(duration_unit IN ('day', 'week', 'month', 'year') "
        "AND duration_value IS NOT NULL AND duration_value > 0)",
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("payment_request_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_price_id", sa.Uuid(), nullable=False),
        sa.Column("payment_method_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("currency_code", sa.String(length=16), nullable=False),
        sa.Column("regular_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("discount_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("final_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("promotion_id", sa.Uuid(), nullable=True),
        sa.Column("duration_value", sa.Integer(), nullable=True),
        sa.Column("duration_unit", sa.String(length=16), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('manual_settlement')",
            name="ck_payments_provider",
        ),
        sa.CheckConstraint("regular_amount > 0", name="ck_payments_regular_positive"),
        sa.CheckConstraint(
            "discount_amount >= 0",
            name="ck_payments_discount_nonnegative",
        ),
        sa.CheckConstraint("final_amount > 0", name="ck_payments_final_positive"),
        sa.CheckConstraint(
            "final_amount = regular_amount - discount_amount",
            name="ck_payments_amount_math",
        ),
        sa.CheckConstraint(
            "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day', 'week', 'month', 'year') "
            "AND duration_value IS NOT NULL AND duration_value > 0)",
            name="ck_payments_duration",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_request_id", "project_id"],
            ["payment_requests.id", "payment_requests.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_request_project",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_plan_project",
        ),
        sa.ForeignKeyConstraint(
            ["plan_price_id", "project_id", "plan_id", "currency_code"],
            [
                "plan_prices.id",
                "plan_prices.project_id",
                "plan_prices.plan_id",
                "plan_prices.currency_code",
            ],
            ondelete="RESTRICT",
            name="fk_payments_price_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_method_project",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["telegram_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_payments_id_project"),
        sa.UniqueConstraint("payment_request_id", name="uq_payments_payment_request"),
    )
    op.create_index("ix_payments_project_id", "payments", ["project_id"])
    op.create_index("ix_payments_payment_request_id", "payments", ["payment_request_id"])
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_payment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'cancelled')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint(
            "current_period_end IS NULL OR current_period_end > current_period_start",
            name="ck_subscriptions_period_order",
        ),
        sa.CheckConstraint(
            "started_at <= current_period_start",
            name="ck_subscriptions_started_before_period",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_subscriptions_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            name="fk_subscriptions_plan_project",
        ),
        sa.ForeignKeyConstraint(
            ["last_payment_id", "project_id"],
            ["payments.id", "payments.project_id"],
            name="fk_subscriptions_last_payment_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_subscriptions_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "customer_id",
            "plan_id",
            name="uq_subscriptions_project_customer_plan",
        ),
    )
    op.create_index("ix_subscriptions_project_id", "subscriptions", ["project_id"])
    op.create_index("ix_subscriptions_customer_id", "subscriptions", ["customer_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table(
        "entitlement_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('payment', 'manual', 'owner_service', 'promocode', 'trial', "
            "'referral', 'import', 'compensation')",
            name="ck_entitlement_grants_source",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_entitlement_grants_status",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_entitlement_grants_validity",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= valid_from",
            name="ck_entitlement_grants_revoked_at",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_entitlement_grants_status_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_entitlement_grants_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_entitlement_grants_resource_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_entitlement_grants_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "source",
            "source_id",
            "resource_id",
            name="uq_entitlement_grants_source_resource",
        ),
    )
    op.create_index(
        "ix_entitlement_grants_project_id",
        "entitlement_grants",
        ["project_id"],
    )
    op.create_index(
        "ix_entitlement_grants_customer_id",
        "entitlement_grants",
        ["customer_id"],
    )
    op.create_index(
        "ix_entitlement_grants_resource_id",
        "entitlement_grants",
        ["resource_id"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=180), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('access_recalculate')",
            name="ck_outbox_events_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', "
            "'failed_retryable', 'failed_permanent')",
            name="ck_outbox_events_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_outbox_events_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_outbox_events_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "dedupe_key",
            name="uq_outbox_events_project_dedupe",
        ),
    )
    op.create_index("ix_outbox_events_project_id", "outbox_events", ["project_id"])
    op.create_index(
        "ix_outbox_events_status_available",
        "outbox_events",
        ["status", "available_at"],
    )

    for table_name in (
        "payments",
        "subscriptions",
        "entitlement_grants",
        "outbox_events",
    ):
        _enable_project_rls(table_name)


def downgrade() -> None:
    for table_name in (
        "outbox_events",
        "entitlement_grants",
        "subscriptions",
        "payments",
    ):
        op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_policy ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_index("ix_outbox_events_project_id", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_entitlement_grants_resource_id", table_name="entitlement_grants")
    op.drop_index("ix_entitlement_grants_customer_id", table_name="entitlement_grants")
    op.drop_index("ix_entitlement_grants_project_id", table_name="entitlement_grants")
    op.drop_table("entitlement_grants")

    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_customer_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_project_id", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_payments_customer_id", table_name="payments")
    op.drop_index("ix_payments_payment_request_id", table_name="payments")
    op.drop_index("ix_payments_project_id", table_name="payments")
    op.drop_table("payments")

    op.drop_constraint(
        "ck_payment_requests_duration",
        "payment_requests",
        type_="check",
    )
    op.drop_column("payment_requests", "duration_unit")
    op.drop_column("payment_requests", "duration_value")
