"""Saved, tenant-isolated broadcasts and durable per-recipient delivery journal.

Revision ID: 0032_tenant_broadcasts
Revises: 0031_financial_record_integrity
"""
# Generated DDL columns are intentionally kept inline for schema review.
# ruff: noqa: E501
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_tenant_broadcasts"
down_revision: str | None = "0031_financial_record_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "broadcast_audience", "broadcast_templates", "broadcast_drafts",
    "broadcast_campaigns", "broadcast_deliveries",
)


def upgrade() -> None:
    op.create_table(
        "broadcast_audience",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("telegram_bot_id", sa.BigInteger(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("first_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active','blocked')", name="ck_broadcast_audience_status"),
        sa.CheckConstraint("telegram_user_id > 0 AND telegram_bot_id > 0", name="ck_broadcast_audience_ids"),
    )
    op.create_index(
        "ix_broadcast_audience_active", "broadcast_audience",
        ["project_id", "telegram_bot_id", "status"],
    )
    op.create_table(
        "broadcast_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("telegram_users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("entities", sa.JSON()),
        sa.Column("file_id", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("id", "project_id", name="uq_broadcast_template_project"),
        sa.CheckConstraint("kind IN ('text','photo','video','document','animation')", name="ck_broadcast_template_kind"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_broadcast_template_status"),
    )
    op.create_index("ix_broadcast_templates_recent", "broadcast_templates",
                    ["project_id", "created_at"])
    op.create_table(
        "broadcast_drafts",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("template_id", sa.Uuid()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id", "project_id"],
            ["broadcast_templates.id", "broadcast_templates.project_id"],
            name="fk_broadcast_draft_project_template",
        ),
        sa.CheckConstraint("mode IN ('create','replace','rename')", name="ck_broadcast_draft_mode"),
    )
    op.create_table(
        "broadcast_campaigns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("telegram_users.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("entities", sa.JSON()),
        sa.Column("file_id", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "project_id", name="uq_broadcast_campaign_project"),
        sa.ForeignKeyConstraint(
            ["template_id", "project_id"],
            ["broadcast_templates.id", "broadcast_templates.project_id"],
            name="fk_broadcast_campaign_project_template",
        ),
        sa.CheckConstraint("status IN ('running','paused','completed')", name="ck_broadcast_campaign_status"),
        sa.CheckConstraint("kind IN ('text','photo','video','document','animation')", name="ck_broadcast_campaign_kind"),
    )
    op.create_index("ix_broadcast_campaigns_project_status",
                    "broadcast_campaigns", ["project_id", "status", "created_at"])
    op.create_table(
        "broadcast_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(48)),
        sa.ForeignKeyConstraint(
            ["campaign_id", "project_id"],
            ["broadcast_campaigns.id", "broadcast_campaigns.project_id"],
            ondelete="CASCADE", name="fk_broadcast_delivery_project_campaign",
        ),
        sa.UniqueConstraint("campaign_id", "telegram_user_id", name="uq_broadcast_delivery_recipient"),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','blocked','failed','unknown')",
            name="ck_broadcast_delivery_status",
        ),
    )
    op.create_index("ix_broadcast_delivery_due", "broadcast_deliveries",
                    ["project_id", "status", "next_attempt_at"])

    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        # No cross-tenant visibility, even for service workers with no user
        # context. No project scope -> zero broadcast rows.
        op.execute(f"""
            CREATE POLICY {table}_project_scope ON public.{table}
            FOR ALL
            USING (project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid)
            WITH CHECK (project_id = NULLIF(current_setting('app.current_project_id', true), '')::uuid)
        """)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
