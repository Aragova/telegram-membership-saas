"""Tenant-scoped owner-notification settings and delivery ledger.

Revision ID: 0025_owner_notifications
Revises: 0024_owner_currency_selection
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_owner_notifications"
down_revision: str | None = "0024_owner_currency_selection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "owner_notification_settings",
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("invoice_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("receipt_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("decision_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expiry_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("access_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("daily_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "owner_notification_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.UniqueConstraint(
            "project_id", "kind", "event_key", "telegram_id",
            name="uq_owner_notification_delivery_event_recipient",
        ),
        sa.CheckConstraint(
            "kind IN ('invoice', 'expiry', 'expired', 'access', 'recovered', 'daily')",
            name="ck_owner_notification_delivery_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed')",
            name="ck_owner_notification_delivery_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_owner_notification_delivery_attempts"),
    )
    op.create_index(
        "ix_owner_notification_delivery_ready", "owner_notification_deliveries",
        ["project_id", "status", "available_at"],
    )
    for table in ("owner_notification_settings", "owner_notification_deliveries"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_project_scope ON {table}
            USING ({PROJECT} IS NOT NULL AND project_id = {PROJECT})
            WITH CHECK ({PROJECT} IS NOT NULL AND project_id = {PROJECT})
        """)
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON owner_notification_settings, owner_notification_deliveries
            TO membership_runtime;
          END IF;
        END $$;
    """)


def downgrade() -> None:
    op.drop_table("owner_notification_deliveries")
    op.drop_table("owner_notification_settings")
