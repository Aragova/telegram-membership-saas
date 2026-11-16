"""Backup GCS freshness watch, durable operator-only notifications, and host receipts.

Revision ID: 0088_backup_monitor
Revises: 0087_saas_review_result_notice
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088_backup_monitor"
down_revision: str | None = "0087_saas_review_result_notice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only the root-owned systemd reporter, via local PostgreSQL as the
    # migration/owner membership role, writes this non-secret receipt.
    op.create_table(
        "platform_backup_attempt_receipt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_backup_receipt_singleton"),
        sa.CheckConstraint(
            "result IN ('success','failed')", name="ck_backup_receipt_result"
        ),
    )
    op.execute("REVOKE ALL ON public.platform_backup_attempt_receipt FROM PUBLIC")
    op.create_table(
        "platform_backup_watch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_complete_at", sa.DateTime(timezone=True)),
        sa.Column("suspected_since", sa.DateTime(timezone=True)),
        sa.Column("incident_id", sa.Uuid()),
        sa.Column("last_resolved_incident_id", sa.Uuid()),
        sa.CheckConstraint("id = 1", name="ck_backup_watch_singleton"),
        sa.CheckConstraint(
            "state IN ('healthy','stale','failed','verify_error')",
            name="ck_backup_watch_state",
        ),
    )
    op.create_table(
        "platform_backup_alert_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "incident_id", "kind", name="uq_backup_alert_incident_kind"
        ),
        sa.CheckConstraint("kind IN ('problem','recovered')", name="ck_backup_alert_kind"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','cancelled')",
            name="ck_backup_alert_status",
        ),
    )
    op.create_index(
        "ix_backup_alert_due", "platform_backup_alert_outbox",
        ["available_at", "id"],
        postgresql_where=sa.text("status IN ('pending','sending')"),
    )
    for table in ("platform_backup_watch", "platform_backup_alert_outbox"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        for action in ("SELECT", "INSERT", "UPDATE"):
            op.execute(f"""
                CREATE POLICY backup_operator_{action.lower()}
                ON public.{table} FOR {action}
                {"USING (public.platform_monitor_is_operator())" if action != "INSERT" else ""}
                {"WITH CHECK (public.platform_monitor_is_operator())" if action != "SELECT" else ""}
            """)
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
            THEN
                GRANT SELECT ON public.platform_backup_attempt_receipt
                    TO membership_runtime;
                GRANT SELECT, INSERT, UPDATE ON
                    public.platform_backup_watch,
                    public.platform_backup_alert_outbox TO membership_runtime;
            END IF;
        END $grant$
    """)


def downgrade() -> None:
    op.drop_table("platform_backup_alert_outbox")
    op.drop_table("platform_backup_watch")
    op.drop_table("platform_backup_attempt_receipt")
