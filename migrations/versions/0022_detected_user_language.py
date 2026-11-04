"""Remember last incoming Telegram language for recipient-specific notifications.

Revision ID: 0022_detected_user_language
Revises: 0021_user_language
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_detected_user_language"
down_revision: str | None = "0021_user_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_users",
        sa.Column("detected_language", sa.String(2), nullable=True),
    )
    op.create_check_constraint(
        "ck_telegram_users_detected_language",
        "telegram_users",
        "detected_language IS NULL OR detected_language IN ('ru', 'en')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_telegram_users_detected_language",
        "telegram_users", type_="check",
    )
    op.drop_column("telegram_users", "detected_language")
