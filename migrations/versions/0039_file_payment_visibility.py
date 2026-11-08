"""Per-bot switches for file sales without touching subscription methods.

Revision ID: 0039_file_payment_visibility
Revises: 0038_file_manual_checkout
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_file_payment_visibility"
down_revision: str | None = "0038_file_manual_checkout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_sale_method_visibility",
        sa.Column(
            "project_id", sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "telegram_bot_id", sa.BigInteger(),
            sa.ForeignKey("bot_instances.telegram_bot_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("method_id", sa.Uuid(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute(
        "ALTER TABLE public.file_sale_method_visibility ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.file_sale_method_visibility FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        CREATE POLICY file_sale_method_visibility_project_scope
        ON public.file_sale_method_visibility FOR ALL
        USING (
            project_id = NULLIF(
                current_setting('app.current_project_id', true), ''
            )::uuid
        )
        WITH CHECK (
            project_id = NULLIF(
                current_setting('app.current_project_id', true), ''
            )::uuid
        )
    """)


def downgrade() -> None:
    op.drop_table("file_sale_method_visibility")
