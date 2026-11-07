"""Support grouped Telegram photo/video media in reusable broadcasts.

Revision ID: 0033_broadcast_media_albums
Revises: 0032_tenant_broadcasts
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_broadcast_media_albums"
down_revision: str | None = "0032_tenant_broadcasts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("broadcast_templates", "broadcast_campaigns"):
        op.add_column(table, sa.Column("media_items", sa.JSON(), nullable=True))
        op.drop_constraint(f"ck_broadcast_{'template' if table == 'broadcast_templates' else 'campaign'}_kind", table, type_="check")
        op.create_check_constraint(
            f"ck_broadcast_{'template' if table == 'broadcast_templates' else 'campaign'}_kind",
            table, "kind IN ('text','photo','video','document','animation','album')",
        )


def downgrade() -> None:
    for table in ("broadcast_campaigns", "broadcast_templates"):
        # Never erase saved posts, campaign history or delivery evidence.
        # A rollback with album content must be postponed until it is
        # explicitly migrated or archived using a safe recovery plan.
        conn = op.get_bind()
        present = conn.execute(sa.text(
            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE kind = 'album')"
        )).scalar()
        if present:
            raise RuntimeError("Cannot downgrade broadcast albums with existing data")
        op.drop_constraint(f"ck_broadcast_{'template' if table == 'broadcast_templates' else 'campaign'}_kind", table, type_="check")
        op.create_check_constraint(
            f"ck_broadcast_{'template' if table == 'broadcast_templates' else 'campaign'}_kind",
            table, "kind IN ('text','photo','video','document','animation')",
        )
        op.drop_column(table, "media_items")
