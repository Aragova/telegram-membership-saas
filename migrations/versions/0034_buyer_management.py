"""Project-local customer bans and auditable complimentary plan grants.

Revision ID: 0034_buyer_management
Revises: 0033_broadcast_media_albums
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_buyer_management"
down_revision: str | None = "0033_broadcast_media_albums"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_customers",
        sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "project_customers",
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_customers",
        sa.Column("banned_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_customers_banned_by", "project_customers",
        "telegram_users", ["banned_by_user_id"], ["id"], ondelete="RESTRICT",
    )
    op.add_column(
        "entitlement_grants",
        sa.Column("manual_plan_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_entitlement_grants_manual_plan_project",
        "entitlement_grants", "plans",
        ["manual_plan_id", "project_id"], ["id", "project_id"],
    )


def downgrade() -> None:
    # A reversal cannot discard an active block or recorded plan activation.
    # Never silently change a buyer's moderation/access state.
    conn = op.get_bind()
    active_bans = conn.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM project_customers WHERE is_banned)"
    )).scalar()
    manual_plans = conn.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM entitlement_grants "
        "WHERE manual_plan_id IS NOT NULL)"
    )).scalar()
    if active_bans or manual_plans:
        raise RuntimeError(
            "Cannot downgrade while customer bans or manual-plan history exist"
        )
    op.drop_constraint(
        "fk_entitlement_grants_manual_plan_project", "entitlement_grants",
        type_="foreignkey",
    )
    op.drop_column("entitlement_grants", "manual_plan_id")
    op.drop_constraint(
        "fk_project_customers_banned_by", "project_customers",
        type_="foreignkey",
    )
    op.drop_column("project_customers", "banned_by_user_id")
    op.drop_column("project_customers", "banned_at")
    op.drop_column("project_customers", "is_banned")
