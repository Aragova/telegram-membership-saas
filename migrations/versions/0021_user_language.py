"""Store optional, cross-bot per-user language preference.

Revision ID: 0021_user_language
Revises: 0020_platform_usage_metering
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_user_language"
down_revision: str | None = "0020_platform_usage_metering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_users",
        sa.Column("preferred_language", sa.String(2), nullable=True),
    )
    op.create_check_constraint(
        "ck_telegram_users_preferred_language",
        "telegram_users",
        "preferred_language IS NULL OR preferred_language IN ('ru', 'en')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_telegram_users_preferred_language",
        "telegram_users", type_="check",
    )
    op.drop_column("telegram_users", "preferred_language")
