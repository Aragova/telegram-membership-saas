"""Optional Telegram channel-owned Stars checkout link per immutable XTR price.

Default off. Existing invoices, grants, balances, payment ledgers and owner
commission policies remain untouched; native channel sales are NOT bot sales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0095_native_channel_stars"
down_revision: str | None = "0094_retired_bot_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plan_prices", sa.Column(
        "native_subscription_link", sa.String(length=512), nullable=True,
    ))
    op.add_column("plan_prices", sa.Column(
        "native_subscription_chat_id", sa.String(length=128), nullable=True,
    ))
    op.add_column("plan_prices", sa.Column(
        "native_subscription_bot_id", sa.BigInteger(), nullable=True,
    ))
    op.add_column("plan_prices", sa.Column(
        "native_subscription_active", sa.Boolean(),
        nullable=False, server_default=sa.text("false"),
    ))
    op.create_check_constraint(
        "ck_plan_prices_native_stars_complete", "plan_prices",
        "NOT native_subscription_active OR "
        "(native_subscription_link IS NOT NULL AND "
        "native_subscription_chat_id IS NOT NULL AND "
        "native_subscription_bot_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_plan_prices_native_stars_complete", "plan_prices", type_="check",
    )
    for column in (
        "native_subscription_active", "native_subscription_bot_id",
        "native_subscription_chat_id", "native_subscription_link",
    ):
        op.drop_column("plan_prices", column)
